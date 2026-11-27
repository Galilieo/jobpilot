"""用两次真实改写流水线证明投递固定了当时的事实、岗位要求和简历版本。"""

import asyncio
from uuid import UUID

import httpx

from jobpilot.api.deps import get_model_client
from jobpilot.api.tailoring import RunLauncher
from jobpilot.db.models import AiRun, Fact
from jobpilot.main import app
from jobpilot.services.llm_gateway import ModelBudget
from jobpilot.services.tailoring_graph import TailorDeps, run_tailoring
from jobpilot.tests.test_applications_api import save_application
from jobpilot.tests.test_call_accounting import fake_upstream
from jobpilot.tests.test_tailor_acceptance import (
    CORRECTED,
    OLD_SENTENCE,
    StubbornModel,
    seed,
)
from jobpilot.tests.test_tailoring_api import launcher as launcher
from jobpilot.tests.test_tailoring_api import start
from jobpilot.tests.test_tailoring_graph import Factory

_FIXTURE_PASSWORD = "Synthetic-Application-Scenario-2026"


async def _publish_corrected_fact(
    client: httpx.AsyncClient,
    previous: Fact,
) -> tuple[dict, str]:
    edited = await client.put(
        f"/api/facts/{previous.lineage_id}",
        json={"text": CORRECTED, "confirmed": True, "expected_revision": 0},
    )
    assert edited.is_success, edited.text
    committed = await client.post(
        "/api/facts/commit",
        json={
            "base_fact_set_id": str(previous.fact_set_id),
            "expected_revision": edited.json()["revision"],
        },
    )
    assert committed.is_success, committed.text
    assert committed.json()["version"] == 4
    fact = next(
        item
        for item in (await client.get("/api/facts")).json()["facts"]
        if item["lineage_id"] == str(previous.lineage_id)
    )
    return fact, committed.json()["fact_set_id"]


async def _first_run(session_factory: Factory) -> tuple[AiRun, Fact]:
    user_id, run_id, fact_id = await seed(
        session_factory,
        fact_text=OLD_SENTENCE,
        fact_set_version=3,
        password=_FIXTURE_PASSWORD,
    )
    async with fake_upstream(StubbornModel(fact_id)) as model:
        await run_tailoring(
            TailorDeps(
                session_factory=session_factory,
                client=model,
                budget=ModelBudget(remaining_calls=30, remaining_tokens=300_000),
                limiter=asyncio.Semaphore(4),
            ),
            user_id,
            run_id,
        )
    async with session_factory() as session:
        return await session.get_one(AiRun, run_id), await session.get_one(
            Fact, fact_id
        )


async def _second_run(
    client: httpx.AsyncClient,
    runner: RunLauncher,
    job_id: UUID,
    current_fact: dict,
) -> tuple[str, dict]:
    job = await client.put(
        f"/api/jobs/{job_id}",
        json={"requirements": [{"type": "hard", "text": "接口延迟优化经验"}]},
    )
    assert job.is_success
    requirement = job.json()["parsed"]["requirements"][0]
    async with fake_upstream(
        StubbornModel(UUID(current_fact["fact_id"]), requirement["requirement_id"])
    ) as model:
        app.dependency_overrides[get_model_client] = lambda: model
        started = await start(client, job_id)
        assert started.status_code == 202, started.text
        await runner.wait_all()
    run_id = started.json()["run_id"]
    response = await client.get(f"/api/tailor-runs/{run_id}/resume")
    assert response.is_success, response.text
    return run_id, response.json()


async def test_v3_application_retains_30_percent_after_v4_rerun(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    launcher: RunLauncher,
) -> None:
    client = async_client
    old_run, old_fact = await _first_run(session_factory)
    login = await client.post(
        "/api/auth/login",
        json={"email": "accept@tailor.invalid", "password": _FIXTURE_PASSWORD},
    )
    assert login.is_success
    job_id = old_run.user_job_id
    old_resume_path = f"/api/tailor-runs/{old_run.run_id}/resume"
    old_resume = (await client.get(old_resume_path)).json()
    application = await save_application(client, str(job_id), old_resume["version_id"])
    path = f"/api/applications/{application['application_id']}"
    old_html = (await client.get(f"{path}/snapshot.html")).text
    assert "30%" in old_html and application["fact_set_id"] == str(old_fact.fact_set_id)

    new_fact, new_fact_set_id = await _publish_corrected_fact(client, old_fact)
    run_id, new_resume = await _second_run(client, launcher, job_id, new_fact)
    await _assert_run_history(client, session_factory, old_run, run_id, new_fact_set_id)
    exported = (await client.get(f"/api/tailor-runs/{run_id}/export")).text
    assert "20%" in exported and "30%" not in exported

    changed = await client.put(
        path, json={"expected_revision": 1, "stage": "written_test"}
    )
    assert changed.is_success
    unchanged = await client.get(path)
    assert unchanged.json()["snapshot_json"] == old_resume["content"]
    assert unchanged.json()["resume_version_id"] == old_resume["version_id"]
    local_html = old_html.replace("30%", "40%")
    assert (
        "40%" in local_html
        and (await client.get(f"{path}/snapshot.html")).text == old_html
    )

    compared = (await client.get(f"{path}/diff")).json()
    assert (
        compared["regenerated"]
        and compared["regenerated_resume_id"] == new_resume["resume_id"]
    )
    assert compared["resume_changes"] == []
    risk = next(risk for risk in compared["risks"] if risk["reason"] == "fact_changed")
    assert "30%" in risk["bullet_text"]
    assert risk["facts"][0]["before"] == OLD_SENTENCE
    assert risk["facts"][0]["after"] == CORRECTED


async def _assert_run_history(
    client: httpx.AsyncClient,
    session_factory: Factory,
    old_run: AiRun,
    new_run_id: str,
    new_fact_set_id: str,
) -> None:
    async with session_factory() as session:
        new_run = await session.get_one(AiRun, UUID(new_run_id))
        reread = await session.get_one(AiRun, old_run.run_id)
    assert reread.fact_set_id == old_run.fact_set_id
    assert reread.job_snapshot_json["requirements"][0]["text"] == "有性能优化经验"
    assert new_run.job_snapshot_json["requirements"][0]["text"] == "接口延迟优化经验"
    assert str(new_run.fact_set_id) == new_fact_set_id
    list_path = f"/api/jobs/{old_run.user_job_id}/matches"
    reports = (await client.get(list_path)).json()
    assert [report["fact_set_version"] for report in reports] == [4, 3]
    older = (await client.get(f"/api/match-reports/{reports[1]['report_id']}")).json()
    assert older["items"][0]["requirement_text"] == "有性能优化经验"
    assert older["items"][0]["facts"][0]["text"] == OLD_SENTENCE
