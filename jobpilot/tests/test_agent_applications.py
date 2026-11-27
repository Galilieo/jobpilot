"""以假上游和真实确认接口验证版本绑定、重放及撤销。"""

from datetime import UTC, datetime, timedelta
from json import loads
from uuid import UUID

import httpx
from sqlalchemy import select

from jobpilot.api.deps import get_model_client
from jobpilot.db.models import AiRun, TailoredResume
from jobpilot.main import app
from jobpilot.tests.test_agent_api import batch, card_of, done_of, message_body, reply
from jobpilot.tests.test_applications_api import presented_resume as presented_resume
from jobpilot.tests.test_applications_api import save_application
from jobpilot.tests.test_call_accounting import fake_upstream
from jobpilot.tests.test_tailored_resumes_api import _seed_tailored_resume
from jobpilot.tests.test_tailoring_graph import Factory


async def test_agent_card_binds_version_and_undo_requires_created_revision(
    async_client: httpx.AsyncClient,
    presented_resume: tuple[UUID, str, dict],
) -> None:
    _, job_id, version = presented_resume
    conversation = await async_client.post(
        "/api/conversations", json={"current_job_id": job_id}
    )
    path = f"/api/conversations/{conversation.json()['conversation_id']}"
    async with fake_upstream(_record_application_reply) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        first_card = await _application_card(async_client, path)
        assert first_card["args"]["resume_version_id"] == version["version_id"]
        current = await async_client.get("/api/tailored-resumes")
        run_id = current.json()[0]["run_id"]
        saved = await async_client.put(
            f"/api/tailor-runs/{run_id}/resume",
            json={
                "base_version_id": version["version_id"],
                "content": {**version["content"], "header": ["第二版"]},
            },
        )
        assert saved.is_success, saved.text
        application_id = await _confirm_application_card(
            async_client, path, first_card["action_id"]
        )
        repeated_id = await _confirm_application_card(
            async_client, path, first_card["action_id"]
        )
        assert repeated_id == application_id
        assert len((await async_client.get("/api/applications")).json()) == 1
        response = await async_client.get(f"/api/applications/{application_id}")
        assert response.json()["resume_version_id"] == version["version_id"]
        assert response.json()["snapshot_json"] == version["content"]
        assert (
            await async_client.post(f"/api/actions/{first_card['action_id']}/undo")
        ).is_success
        assert (
            await async_client.get(f"/api/applications/{application_id}")
        ).status_code == 404

        second_card = await _application_card(async_client, path)
        second_id = await _confirm_application_card(
            async_client, path, second_card["action_id"]
        )
        changed = await async_client.put(
            f"/api/applications/{second_id}",
            json={"stage": "written_test", "expected_revision": 1},
        )
        assert changed.is_success
        late_undo = await async_client.post(
            f"/api/actions/{second_card['action_id']}/undo"
        )
        assert late_undo.status_code == 409


def _record_application_reply(request: httpx.Request) -> httpx.Response:
    if loads(request.content)["messages"][-1]["role"] == "tool":
        return reply()
    return batch(("record_application", {"channel": "官网"}))


async def test_latest_resume_uses_completion_order_for_card_job_state_list_and_diff(
    async_client: httpx.AsyncClient,
    presented_resume: tuple[UUID, str, dict],
    session_factory: Factory,
) -> None:
    user_id, job_id, latest = presented_resume
    async with session_factory.begin() as db:
        earlier_started = await db.scalar(
            select(AiRun).where(
                AiRun.user_job_id == UUID(job_id), AiRun.kind == "tailor"
            )
        )
        now = datetime.now(UTC)
        _, earlier_completed_id = await _seed_tailored_resume(
            db,
            user_id,
            doc_id=earlier_started.base_doc_id,
            fact_set_id=earlier_started.fact_set_id,
            job_title="先完成的改写",
            job_company="示例公司",
            version_number=1,
            created_at=now - timedelta(days=1),
        )
        earlier_completed = await db.get_one(AiRun, earlier_completed_id)
        earlier_completed.user_job_id = UUID(job_id)
        earlier_completed.started_at = now - timedelta(days=2)
        earlier_started.started_at = now - timedelta(days=3)
        former = await db.scalar(
            select(TailoredResume).where(TailoredResume.run_id == earlier_completed_id)
        )
        former.created_at = earlier_completed.finished_at
    old_application = await save_application(
        async_client, job_id, str(former.current_version_id)
    )
    ranked = (await async_client.get(f"/api/jobs/{job_id}/tailor-runs")).json()
    assert [row["run_id"] for row in ranked] == [
        str(earlier_started.run_id),
        str(earlier_completed_id),
    ]
    compared = await async_client.get(
        f"/api/applications/{old_application['application_id']}/diff"
    )
    assert compared.json()["regenerated_resume_id"] == latest["resume_id"]
    conversation = await async_client.post(
        "/api/conversations", json={"current_job_id": job_id}
    )
    path = f"/api/conversations/{conversation.json()['conversation_id']}"

    def respond(request: httpx.Request) -> httpx.Response:
        system = loads(request.content)["messages"][0]["content"]
        assert f'"resume":{{"run_id":"{earlier_started.run_id}"' in system
        return _record_application_reply(request)

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        card = await _application_card(async_client, path)
    assert card["args"]["resume_version_id"] == latest["version_id"]
    assert (
        datetime.fromisoformat(card["args"]["resume_generated_at"])
        == earlier_started.finished_at
    )


async def _application_card(client: httpx.AsyncClient, path: str) -> dict:
    sent = await client.post(f"{path}/messages", json=message_body("我投了"))
    return card_of(sent)


async def _confirm_application_card(
    client: httpx.AsyncClient, path: str, action_id: str
) -> str:
    confirmed = await client.post(
        f"{path}/actions/{action_id}", json={"decision": "confirm"}
    )
    assert confirmed.is_success, confirmed.text
    return done_of(confirmed)["action"]["result"]["application_id"]
