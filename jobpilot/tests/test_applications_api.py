"""用真实 HTTP 接口验证投递、阶段和快照的事务行为。"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from jobpilot.db.conversations import find_conversation
from jobpilot.db.jobs import get_user_job
from jobpilot.db.models import (
    AiRun,
    Application,
    ApplicationStageEvent,
    User,
)
from jobpilot.services.accounts import PurgeResult, purge_accounts
from jobpilot.tests.test_agent_resume_actions import _resume
from jobpilot.tests.test_facts_api import new_idempotency_headers
from jobpilot.tests.test_jobs_api import sign_in
from jobpilot.tests.test_tailoring_graph import Factory


async def test_application_routes_require_login(
    async_client: httpx.AsyncClient,
) -> None:
    response = await async_client.get("/api/applications")
    assert response.status_code == 401


async def test_missing_owned_application_returns_404(
    async_client: httpx.AsyncClient,
) -> None:
    await sign_in(async_client, "unknown-application@example.invalid")
    response = await async_client.put(
        f"/api/applications/{uuid4()}", json={"expected_revision": 1, "stage": "offer"}
    )
    assert response.status_code == 404


@pytest.fixture
async def presented_resume(
    async_client: httpx.AsyncClient, session_factory: Factory
) -> tuple[UUID, str, dict]:
    resume = await _resume(
        async_client, session_factory, "application@example.invalid", with_citation=True
    )
    async with session_factory() as session:
        run = await session.get(AiRun, resume.run_id)
        job_id = str(run.user_job_id)
    return resume.user_id, job_id, resume.current


async def save_application(
    client: httpx.AsyncClient,
    job_id: str,
    resume_version: str,
    day: str = "2026-11-25",
) -> dict:
    response = await client.post(
        "/api/applications",
        json={"job_id": job_id, "resume_version_id": resume_version, "applied_at": day},
        headers=new_idempotency_headers(),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_application_freezes_selected_resume_and_survives_job_deletion(
    async_client: httpx.AsyncClient,
    presented_resume: tuple[UUID, str, dict],
) -> None:
    _, job_id, version = presented_resume
    job_path = f"/api/jobs/{job_id}"
    job = await async_client.put(job_path, json={"basic_fields": {"title": "用户改名"}})
    assert job.is_success
    headers = new_idempotency_headers()
    payload = {"job_id": job_id, "resume_version_id": version["version_id"]}
    first = await async_client.post("/api/applications", json=payload, headers=headers)
    replay = await async_client.post("/api/applications", json=payload, headers=headers)
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    path = f"/api/applications/{first.json()['application_id']}"
    snapshot = await async_client.get(f"{path}/snapshot.html")
    assert snapshot.is_success
    assert "frame-ancestors 'self'" in snapshot.headers["Content-Security-Policy"]
    assert "测试同学" in snapshot.text
    assert "<script" not in snapshot.text
    assert "@media screen and (max-width: 210mm)" in snapshot.text
    assert "@page { size: A4; margin: 0; }" in snapshot.text
    assert (await async_client.delete(job_path)).status_code == 204
    detail = (await async_client.get(path)).json()
    assert detail["user_job_id"] is detail["resume_version_id"] is None
    assert detail["resume_version"] == 1
    assert detail["snapshot_json"] == version["content"]
    assert detail["job_snapshot_json"]["title"] == "用户改名"
    assert snapshot.text == (await async_client.get(f"{path}/snapshot.html")).text


async def test_deleted_application_cannot_be_recreated_by_old_request(
    async_client: httpx.AsyncClient,
    presented_resume: tuple[UUID, str, dict],
    session_factory: Factory,
) -> None:
    _, job_id, version = presented_resume
    headers = new_idempotency_headers()
    payload = {"job_id": job_id, "resume_version_id": version["version_id"]}
    first = await async_client.post("/api/applications", json=payload, headers=headers)
    assert first.status_code == 201, first.text
    path = f"/api/applications/{first.json()['application_id']}"
    deleted = await async_client.request("DELETE", path, json={"expected_revision": 1})
    assert deleted.status_code == 204
    replay = await async_client.post("/api/applications", json=payload, headers=headers)
    assert replay.status_code == 410
    assert replay.json()["detail"] == "这条记录已经删除，如需重新记录请重新提交"
    async with session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(Application)) == 0
        assert (
            await db.scalar(select(func.count()).select_from(ApplicationStageEvent))
            == 0
        )


async def test_delete_job_returns_conflict_with_bound_conversation_lock(
    async_client: httpx.AsyncClient,
    presented_resume: tuple[UUID, str, dict],
    session_factory: Factory,
) -> None:
    user_id, job_id, _ = presented_resume
    created = await async_client.post(
        "/api/conversations", json={"current_job_id": job_id}
    )
    assert created.status_code == 201, created.text
    conversation_id = UUID(created.json()["conversation_id"])
    async with session_factory.begin() as confirmation:
        assert await find_conversation(
            user_id, confirmation, conversation_id, lock=True
        )
        async with asyncio.timeout(2):
            conflicting = await async_client.delete(f"/api/jobs/{job_id}")
        assert conflicting.status_code == 409
    assert (await async_client.delete(f"/api/jobs/{job_id}")).status_code == 204
    conversation = (
        await async_client.get(f"/api/conversations/{conversation_id}")
    ).json()
    assert conversation["current_job_id"] is None


async def test_second_application_and_stage_events_determine_job_stage(
    async_client: httpx.AsyncClient,
    presented_resume: tuple[UUID, str, dict],
) -> None:
    _, job_id, version = presented_resume
    first = await save_application(async_client, job_id, version["version_id"])
    second = await save_application(
        async_client, job_id, version["version_id"], "2026-11-27"
    )
    first_path = f"/api/applications/{first['application_id']}"
    second_path = f"/api/applications/{second['application_id']}"
    assert (
        await async_client.put(
            first_path, json={"expected_revision": 1, "stage": "rejected"}
        )
    ).is_success
    for revision, stage in [
        (1, "written_test"),
        (2, "interviewing"),
        (3, "interviewing"),
    ]:
        response = await async_client.put(
            second_path,
            json={
                "expected_revision": revision,
                "stage": stage,
                "occurred_at": "2026-11-26",
            },
        )
        assert response.is_success, response.text
    rejected = await async_client.put(
        second_path, json={"expected_revision": 1, "stage": "offer"}
    )
    assert rejected.status_code == 409
    await save_application(async_client, job_id, version["version_id"], "2026-11-20")
    detail = (await async_client.get(second_path)).json()
    assert detail["revision"] == 3
    assert [
        (event["from_stage"], event["to_stage"]) for event in detail["stage_events"]
    ] == [
        (None, "applied"),
        ("applied", "written_test"),
        ("written_test", "interviewing"),
    ]
    assert detail["stage_events"][-1]["to_stage"] == detail["stage"]
    assert detail["stage_events"][-1]["occurred_at"] == "2026-11-26"
    assert detail["snapshot_json"] == version["content"]
    assert (await async_client.get(first_path)).json()["stage"] == "rejected"
    assert (await async_client.get(f"/api/jobs/{job_id}")).json()[
        "stage"
    ] == "interviewing"
    assert (await async_client.get("/api/jobs")).json()[0]["stage"] == "interviewing"
    board = (await async_client.get("/api/overview")).json()["board"]
    assert board["applied"][0]["stage"] == "interviewing"
    archived = await async_client.put(f"/api/jobs/{job_id}", json={"archived": True})
    assert archived.json()["stage"] == "archived"
    restored = await async_client.put(f"/api/jobs/{job_id}", json={"archived": False})
    assert restored.json()["stage"] == "interviewing"


async def test_application_archive_export_and_account_purge(
    async_client: httpx.AsyncClient,
    presented_resume: tuple[UUID, str, dict],
    session_factory: Factory,
) -> None:
    user_id, job_id, version = presented_resume
    application = await save_application(async_client, job_id, version["version_id"])
    archive = (await async_client.get("/api/account/export")).json()
    assert archive["applications"][0]["snapshot_json"] == version["content"]
    assert "snapshot_html" not in archive["applications"][0]
    assert (
        archive["application_stage_events"][0]["application_id"]
        == application["application_id"]
    )
    async with session_factory.begin() as session:
        user = await session.get(User, user_id)
        user.deletion_requested_at = datetime.now(UTC) - timedelta(days=8)
    assert await purge_accounts(session_factory) == PurgeResult(deleted=1, failed=0)
    async with session_factory() as session:
        assert (
            await session.scalar(
                select(Application).where(Application.user_id == user_id)
            )
            is None
        )
        assert (
            await session.scalar(
                select(ApplicationStageEvent).where(
                    ApplicationStageEvent.user_id == user_id
                )
            )
            is None
        )


async def test_record_conflict_with_job_lock_allows_retry_with_same_key(
    async_client: httpx.AsyncClient,
    presented_resume: tuple[UUID, str, dict],
    session_factory: Factory,
) -> None:
    """岗位删除先持有岗位锁时，确认方向会返回冲突，重试仍使用同一幂等键。"""
    user_id, job_id, version = presented_resume
    headers = new_idempotency_headers()
    payload = {"job_id": job_id, "resume_version_id": version["version_id"]}
    async with session_factory.begin() as session:
        assert await get_user_job(session, user_id, UUID(job_id), for_update=True)
        conflict = await async_client.post(
            "/api/applications", json=payload, headers=headers
        )
        assert conflict.status_code == 409, conflict.text
    retried = await async_client.post(
        "/api/applications", json=payload, headers=headers
    )
    assert retried.status_code == 201, retried.text
    assert len((await async_client.get("/api/applications")).json()) == 1


async def test_application_list_stage_changed_at_follows_last_appended_event(
    async_client: httpx.AsyncClient,
    presented_resume: tuple[UUID, str, dict],
) -> None:
    _, job_id, version = presented_resume
    created = await save_application(
        async_client, job_id, version["version_id"], "2026-11-25"
    )
    assert "stage_changed_at" not in created
    app_id = created["application_id"]

    for url in ("/api/applications", f"/api/applications?job_id={job_id}"):
        items = (await async_client.get(url)).json()
        assert len(items) == 1
        assert items[0]["stage_changed_at"] == "2026-11-25"

    updated = await async_client.put(
        f"/api/applications/{app_id}",
        json={
            "expected_revision": 1,
            "stage": "written_test",
            "occurred_at": "2026-11-26",
        },
    )
    assert updated.is_success, updated.text
    assert "stage_changed_at" not in updated.json()
    for url in ("/api/applications", f"/api/applications?job_id={job_id}"):
        assert (await async_client.get(url)).json()[0][
            "stage_changed_at"
        ] == "2026-11-26"

    # 补填一个更早的发生日期后，stage_changed_at 仍取最后追加的阶段记录日期。
    backfilled = await async_client.put(
        f"/api/applications/{app_id}",
        json={
            "expected_revision": 2,
            "stage": "interviewing",
            "occurred_at": "2026-11-20",
        },
    )
    assert backfilled.is_success, backfilled.text
    for url in ("/api/applications", f"/api/applications?job_id={job_id}"):
        assert (await async_client.get(url)).json()[0][
            "stage_changed_at"
        ] == "2026-11-20"


async def test_application_list_query_count_stays_constant(
    async_client: httpx.AsyncClient,
    presented_resume: tuple[UUID, str, dict],
    db_engine: AsyncEngine,
) -> None:
    _, job_id, version = presented_resume

    async def seed(count: int) -> None:
        for index in range(count):
            created = await save_application(
                async_client,
                job_id,
                version["version_id"],
                f"2026-11-{10 + index:02d}",
            )
            updated = await async_client.put(
                f"/api/applications/{created['application_id']}",
                json={
                    "expected_revision": 1,
                    "stage": "written_test",
                    "occurred_at": f"2026-11-{20 + index:02d}",
                },
            )
            assert updated.is_success, updated.text

    def capture_queries() -> tuple[list[str], object]:
        statements: list[str] = []

        def capture(_conn, _cursor, statement, _params, _ctx, _many) -> None:
            statements.append(statement)

        event.listen(db_engine.sync_engine, "before_cursor_execute", capture)
        return statements, capture

    await seed(1)
    q1, cap1 = capture_queries()
    try:
        assert (await async_client.get("/api/applications")).status_code == 200
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", cap1)

    await seed(4)
    q5, cap5 = capture_queries()
    try:
        resp = await async_client.get("/api/applications")
        assert resp.status_code == 200
        assert len(resp.json()) == 5
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", cap5)

    assert len(q5) == len(q1)
    assert sum("FROM application" in stmt for stmt in q5) == 1
