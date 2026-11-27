"""全量导出和彻底删除使用同一套带版本历史的合成数据。"""

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, inspect, select, update
from sqlalchemy.engine import Connection

from jobpilot.api.tailoring import RunLauncher
from jobpilot.config import settings
from jobpilot.db.models import (
    Base,
    FactSetCriterion,
    FactSetEducation,
    ModelCall,
    User,
)
from jobpilot.services.accounts import PurgeResult, purge_accounts
from jobpilot.tests.test_account_lifecycle import EMAIL, _sign_in
from jobpilot.tests.test_auth import login
from jobpilot.tests.test_facts_api import new_idempotency_headers
from jobpilot.tests.test_tailoring_api import launcher as launcher
from jobpilot.tests.test_tailoring_api import model as model
from jobpilot.tests.test_tailoring_api import start
from jobpilot.tests.test_tailoring_graph import (
    Factory,
    FakeModel,
    SeededInputs,
    seed_inputs,
)


@dataclass(frozen=True)
class Portfolio:
    user_id: UUID
    inputs: SeededInputs
    run_id: str


@pytest.fixture
async def portfolio(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    launcher: RunLauncher,  # noqa: F811 — pytest fixture
    model: FakeModel,  # noqa: F811 — pytest fixture
) -> Portfolio:
    user_id = await _sign_in(async_client)
    inputs = await seed_inputs(session_factory, user_id)
    model.fact_ids = inputs.fact_ids
    job_id = inputs.user_job_id
    job_edit = {"note": "本人的岗位备注", "basic_fields": {"title": "本人的岗位标题"}}
    assert (await async_client.put(f"/api/jobs/{job_id}", json=job_edit)).is_success
    run = await start(async_client, job_id)
    assert run.status_code == 202
    run_id = run.json()["run_id"]
    await launcher.wait_all()
    await _save_new_resume_version(async_client, run_id)
    await _record_application(async_client, job_id, run_id)
    await _create_fact_history(async_client, inputs.fact_set_id)
    await _seed_criteria_and_education(session_factory, user_id, inputs.fact_set_id)
    return Portfolio(user_id, inputs, run_id)


async def _seed_criteria_and_education(
    session_factory: Factory, user_id: UUID, fact_set_id: UUID
) -> None:
    from uuid import uuid4

    async with session_factory.begin() as session:
        session.add_all(
            [
                FactSetCriterion(
                    user_id=user_id,
                    fact_set_id=fact_set_id,
                    field="onsite_days_per_week",
                    int_value=4,
                    source="user",
                ),
                FactSetEducation(
                    user_id=user_id,
                    fact_set_id=fact_set_id,
                    education_id=uuid4(),
                    school="澄湖大学",
                    major="计算机科学与技术",
                    degree="bachelor",
                    status="completed",
                    source="user",
                ),
            ]
        )


async def _save_new_resume_version(client: httpx.AsyncClient, run_id: str) -> None:
    path = f"/api/tailor-runs/{run_id}/resume"
    version = (await client.get(path)).json()
    content = version["content"]
    content["header"].insert(0, "归档用新增页头")
    saved = await client.put(
        path, json={"base_version_id": version["version_id"], "content": content}
    )
    assert saved.status_code == 200, saved.text


async def _record_application(
    client: httpx.AsyncClient, job_id: UUID, run_id: str
) -> None:
    selected = await client.get(f"/api/tailor-runs/{run_id}/resume?version=1")
    assert selected.is_success
    response = await client.post(
        "/api/applications",
        json={
            "job_id": str(job_id),
            "resume_version_id": selected.json()["version_id"],
        },
        headers=new_idempotency_headers(),
    )
    assert response.status_code == 201, response.text


async def _create_fact_history(client: httpx.AsyncClient, fact_set_id: UUID) -> None:
    fact = {
        "experience": "合成项目",
        "kind": "action",
        "responsibility": "own",
        "text": "历史事实",
    }
    created = await client.post("/api/facts", json=fact)
    assert created.status_code == 201, created.text
    published = await client.post(
        "/api/facts/commit",
        json={
            "base_fact_set_id": str(fact_set_id),
            "expected_revision": created.json()["revision"],
        },
    )
    assert published.status_code == 200, published.text
    fact["text"] = "尚未发布的草稿"
    assert (await client.post("/api/facts", json=fact)).status_code == 201


async def _download(client: httpx.AsyncClient) -> dict:
    response = await client.get("/api/account/export")
    assert response.status_code == 200
    assert (
        response.headers["content-disposition"]
        == 'attachment; filename="jobpilot-account.json"'
    )
    assert response.headers["cache-control"] == "no-store"
    return response.json()


async def test_export_has_all_versions_and_only_the_signed_in_owners_data(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    portfolio: Portfolio,
) -> None:
    other_email = "other-archive@account.invalid"
    other_id = await _sign_in(async_client, other_email)
    other_inputs = await seed_inputs(session_factory, other_id)
    # 即使请求传了目标身份，选择数据的依据仍然是调用者的 Cookie。
    other_archive = (
        await async_client.get(f"/api/account/export?user_id={portfolio.user_id}")
    ).json()
    assert other_archive["account"]["email"] == other_email
    assert other_archive["resume_versions"] == []
    assert (
        other_archive["applications"] == other_archive["application_stage_events"] == []
    )
    assert other_archive["fact_sets"][0]["fact_set_id"] == str(other_inputs.fact_set_id)

    assert (await login(async_client, EMAIL)).status_code == 204
    archive = await _download(async_client)
    assert set(archive["account"]) == {"user_id", "email", "deletion_requested_at"}
    assert archive["account"]["email"] == EMAIL
    assert {"user_sessions", "model_calls", "idempotency_keys"}.isdisjoint(archive)
    empty = {"conversations", "messages", "agent_actions", "memories"}
    assert all(archive[name] == [] for name in empty)
    assert all(
        archive[name] for name in archive.keys() - empty - {"account", "format_version"}
    )
    assert str(other_id) not in str(archive)
    assert [item["version"] for item in archive["fact_sets"]] == [1, 2]
    assert len(archive["fact_set_criteria"]) == 1
    assert len(archive["fact_set_educations"]) == 1
    assert {fact["fact_set_id"] for fact in archive["facts"]} == {
        fact_set["fact_set_id"] for fact_set in archive["fact_sets"]
    }
    assert archive["fact_drafts"][0]["items_json"][0]["text"] == "尚未发布的草稿"
    assert archive["resume_documents"][0]["doc_id"] == str(portfolio.inputs.doc_id)
    assert "运单" in archive["resume_documents"][0]["raw_text"]
    assert archive["user_jobs"][0]["note"] == "本人的岗位备注"
    assert (
        archive["user_jobs"][0]["field_overrides_json"]["basic_fields"]["title"]
        == "本人的岗位标题"
    )
    assert archive["job_postings"][0]["raw_text"] == "岗位原文"
    assert len(archive["match_reports"]) == 1
    first, second = archive["resume_versions"]
    assert [first["version"], second["version"]] == [1, 2]
    assert "归档用新增页头" not in first["html"] and "归档用新增页头" in second["html"]
    assert "<!doctype html>" in first["html"].lower()
    assert (
        archive["tailored_resumes"][0]["current_version_id"]
        == second["resume_version_id"]
    )


async def test_pending_export_and_restore_preserve_the_full_archive(
    async_client: httpx.AsyncClient,
    portfolio: Portfolio,
) -> None:
    assert (await async_client.get("/api/me")).json()["user_id"] == str(
        portfolio.user_id
    )
    original = await _download(async_client)
    assert (await async_client.delete("/api/account")).status_code == 204
    assert (await login(async_client, EMAIL)).status_code == 204
    pending = await _download(async_client)
    assert pending["account"].pop("deletion_requested_at") is not None
    pending["account"]["deletion_requested_at"] = None
    assert original == pending
    assert (await async_client.post("/api/account/restore")).status_code == 204
    assert await _download(async_client) == original


async def test_purge_cascades_business_rows_and_anonymizes_model_calls(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    portfolio: Portfolio,
) -> None:
    archive = await _download(async_client)
    report_id = archive["match_reports"][0]["report_id"]
    assert (await async_client.delete("/api/account")).status_code == 204
    assert (await login(async_client, EMAIL)).status_code == 204
    pending_token = async_client.cookies[settings.cookie_name]
    await _expire(session_factory, portfolio.user_id)
    other_id = await _sign_in(async_client, "purge-control@account.invalid")
    await seed_inputs(session_factory, other_id)

    assert await purge_accounts(session_factory) == PurgeResult(deleted=1, failed=0)
    assert await purge_accounts(session_factory) == PurgeResult(deleted=0, failed=0)
    assert (await login(async_client, EMAIL)).status_code == 401
    paths = (
        f"/api/resumes/{portfolio.inputs.doc_id}",
        f"/api/jobs/{portfolio.inputs.user_job_id}",
        f"/api/match-reports/{report_id}",
        f"/api/tailor-runs/{portfolio.run_id}/resume",
        f"/api/tailor-runs/{portfolio.run_id}/export?format=html",
        f"/api/tailor-runs/{portfolio.run_id}",
    )
    for path in paths:
        assert (await async_client.get(path)).status_code == 404, path
    assert (await async_client.get("/api/me")).json()["user_id"] == str(other_id)
    async_client.cookies.clear()
    async_client.cookies.set(settings.cookie_name, pending_token)
    assert (await async_client.get("/api/account/export")).status_code == 401
    async with session_factory() as session:
        for table in Base.metadata.tables.values():
            if table is ModelCall.__table__:
                continue
            owner = table.c.owner_id if table.name == "job_posting" else table.c.user_id
            assert (
                await session.scalar(
                    select(owner).where(owner == portfolio.user_id).limit(1)
                )
                is None
            ), table.name
        calls = (await session.scalars(select(ModelCall))).all()
        assert calls and all(call.user_id is call.run_id is None for call in calls)
        assert {call.purpose for call in calls} >= {"match", "rewrite"}


async def _expire(session_factory: Factory, user_id: UUID) -> None:
    async with session_factory.begin() as session:
        await session.execute(
            update(User)
            .where(User.user_id == user_id)
            .values(
                deletion_requested_at=func.now() - timedelta(days=7),
            )
        )


async def test_account_foreign_keys_in_migrated_database(
    session_factory: Factory,
) -> None:
    def rules(connection: Connection) -> dict[str, dict[str, str]]:
        schema = inspect(connection)
        return {
            table.name: {
                fk["referred_table"]: fk["options"].get("ondelete", "NO ACTION")
                for fk in schema.get_foreign_keys(table.name)
            }
            for table in Base.metadata.tables.values()
        }

    async with session_factory() as session:
        foreign_keys = await (await session.connection()).run_sync(rules)
    assert foreign_keys.pop("fact")["fact_set"] == "CASCADE"
    assert foreign_keys.pop("fact_set_criterion")["fact_set"] == "CASCADE"
    assert foreign_keys.pop("fact_set_education")["fact_set"] == "CASCADE"
    assert foreign_keys.pop("model_call")["ai_run"] == "SET NULL"
    assert foreign_keys.pop("message")["ai_run"] == "CASCADE"
    assert foreign_keys.pop("agent_action")["ai_run"] == "CASCADE"
    assert foreign_keys.pop("application_stage_event")["application"] == "CASCADE"
    foreign_keys.pop("user_account")
    assert {
        table: rules.get("user_account") for table, rules in foreign_keys.items()
    } == dict.fromkeys(foreign_keys, "CASCADE")
