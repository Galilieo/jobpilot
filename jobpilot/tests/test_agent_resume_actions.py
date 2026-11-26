"""验证简历卡固定版本、崩溃恢复、撤销和工具对其他用户资源的隔离。"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from json import dumps, loads
from uuid import UUID

import httpx
import pytest

from jobpilot.api.deps import get_model_client
from jobpilot.db.models import AiRun, ResumeDocument
from jobpilot.db.reports import add_match_report
from jobpilot.main import app
from jobpilot.services.agent import graph
from jobpilot.tests.test_agent_api import (
    _restart_runtime,
    batch,
    card_of,
    conversation,
    done_of,
    events,
    message_body,
    reply,
    sign_in,
)
from jobpilot.tests.test_call_accounting import completion_response, fake_upstream
from jobpilot.tests.test_isolation import user_a as user_a
from jobpilot.tests.test_isolation import user_b as user_b
from jobpilot.tests.test_tailored_resumes_api import _seed_tailored_resume
from jobpilot.tests.test_tailoring_graph import (
    SNAPSHOT,
    Factory,
    SeededInputs,
    seed_inputs,
)


@dataclass(frozen=True)
class OwnedResume:
    user_id: UUID
    inputs: SeededInputs
    run_id: UUID
    current: dict

    @property
    def path(self) -> str:
        return f"/api/tailor-runs/{self.run_id}/resume"

    @property
    def bullet_id(self) -> str:
        return self.current["content"]["sections"][0]["entries"][0]["bullets"][0][
            "bullet_id"
        ]


async def _resume(
    client: httpx.AsyncClient,
    factory: Factory,
    email: str,
) -> OwnedResume:
    await sign_in(client, email)
    user_id = UUID((await client.get("/api/me")).json()["user_id"])
    inputs = await seed_inputs(factory, user_id)
    async with factory.begin() as session:
        _, run_id = await _seed_tailored_resume(
            session,
            user_id,
            doc_id=inputs.doc_id,
            fact_set_id=inputs.fact_set_id,
            job_title="虚构后端实习",
            job_company="示例公司",
            version_number=1,
            created_at=datetime.now(UTC),
        )
    response = await client.get(f"/api/tailor-runs/{run_id}/resume")
    assert response.status_code == 200, response.text
    return OwnedResume(user_id, inputs, run_id, response.json())


def _edit_response(resume: OwnedResume, request: httpx.Request) -> httpx.Response:
    payload = loads(request.content)
    if not payload.get("stream"):
        return completion_response(
            dumps({"verdict": "supported", "unsupported_spans": []})
        )
    if payload["messages"][-1]["role"] == "tool":
        return reply()
    return batch(
        (
            "edit_resume_item",
            {
                "run_id": str(resume.run_id),
                "bullet_id": resume.bullet_id,
                "text": "参与接口开发",
            },
        )
    )


async def _manual_save(
    client: httpx.AsyncClient, resume: OwnedResume, source: dict
) -> dict:
    content = {**source["content"], "header": ["本人另存的页头"]}
    response = await client.put(
        resume.path, json={"base_version_id": source["version_id"], "content": content}
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_resume_edit_rejects_stale_confirmation_and_preserves_other_save(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
) -> None:
    resume = await _resume(async_client, session_factory, "stale@example.invalid")
    path = await conversation(async_client)
    async with fake_upstream(lambda req: _edit_response(resume, req)) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        card = card_of(
            await async_client.post(
                f"{path}/messages", json=message_body("提议修改这条经历")
            )
        )
        assert card["args"]["base_version_id"] == resume.current["version_id"]
        assert (await async_client.get(resume.path)).json()[
            "version_id"
        ] == resume.current["version_id"]
        saved = await _manual_save(async_client, resume, resume.current)
        decided = await async_client.post(
            f"{path}/actions/{card['action_id']}", json={"decision": "confirm"}
        )
        assert decided.status_code == 409, decided.text
    assert (await async_client.get(resume.path)).json() == saved
    assert (await async_client.get(path)).json()["pending_action"] == card


async def test_resume_commit_before_checkpoint_replays_once_and_undo_appends_version(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resume = await _resume(
        async_client, session_factory, "restart-edit@example.invalid"
    )
    path = await conversation(async_client)
    original = graph._apply

    async def interrupt_after_commit(agent, state):
        await original(agent, state)
        raise asyncio.CancelledError

    async with fake_upstream(lambda req: _edit_response(resume, req)) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        card = card_of(
            await async_client.post(
                f"{path}/messages", json=message_body("请把完成改成参与")
            )
        )
        assert card["args"]["check"]["layers"] == "full"
        # 上传另一份底稿。确认和历史导出仍以原改写运行的底稿为准。
        async with session_factory.begin() as session:
            session.add(
                ResumeDocument(user_id=resume.user_id, raw_text="新底稿里的独有文字")
            )
        monkeypatch.setattr(graph, "_apply", interrupt_after_commit)
        action_url = f"{path}/actions/{card['action_id']}"
        assert (
            await async_client.post(action_url, json={"decision": "confirm"})
        ).status_code == 200
        applied = (await async_client.get(resume.path)).json()
        assert applied["version"] == 2
        monkeypatch.setattr(graph, "_apply", original)
        await _restart_runtime(session_factory, monkeypatch)
        repeated = await async_client.post(action_url, json={"decision": "confirm"})
        assert (
            done_of(repeated)["action"]["result"]["version_id"] == applied["version_id"]
        )

    after = (await async_client.get(resume.path)).json()
    assert after == applied
    run = (await async_client.get(f"/api/tailor-runs/{resume.run_id}")).json()
    assert run["base_doc_id"] == str(resume.inputs.doc_id)
    html = await async_client.get(f"/api/tailor-runs/{resume.run_id}/export")
    assert html.status_code == 200 and "新底稿里的独有文字" not in html.text
    undo = await async_client.post(f"/api/actions/{card['action_id']}/undo")
    assert undo.status_code == 200, undo.text
    restored = (await async_client.get(resume.path)).json()
    assert restored["version"] == 3
    assert restored["content"] == resume.current["content"]


async def test_tool_ids_and_decisions_cannot_access_other_users(
    user_a: httpx.AsyncClient,
    user_b: httpx.AsyncClient,
    session_factory: Factory,
) -> None:
    resume_a = await _resume(user_a, session_factory, "tool-a@example.invalid")
    resume_b = await _resume(user_b, session_factory, "tool-b@example.invalid")
    path_b = await conversation(user_b)
    path_a = await conversation(user_a)
    async with session_factory.begin() as session:
        report_run = AiRun(
            user_id=resume_b.user_id,
            kind="match",
            status="succeeded",
            user_job_id=resume_b.inputs.user_job_id,
            fact_set_id=resume_b.inputs.fact_set_id,
            job_snapshot_json=SNAPSHOT,
            finished_at=datetime.now(UTC),
        )
        session.add(report_run)
        await session.flush()
        report = add_match_report(session, report_run, items=[], main_gaps=[])
    crosses = (
        ("read_job", {"job_id": str(resume_b.inputs.user_job_id)}),
        ("read_facts", {"fact_set_id": str(resume_b.inputs.fact_set_id)}),
        ("read_match_report", {"report_id": str(report.report_id)}),
        ("read_resume", {"run_id": str(resume_b.run_id), "version": 1}),
        ("set_current_job", {"job_id": str(resume_b.inputs.user_job_id)}),
        ("match_job", {"job_id": str(resume_b.inputs.user_job_id)}),
        (
            "tailor_resume",
            {
                "job_id": str(resume_a.inputs.user_job_id),
                "base_doc_id": str(resume_b.inputs.doc_id),
            },
        ),
        (
            "edit_resume_item",
            {
                "run_id": str(resume_a.run_id),
                "bullet_id": resume_b.bullet_id,
                "text": "越权编辑",
            },
        ),
    )

    def respond(request: httpx.Request) -> httpx.Response:
        last = loads(request.content)["messages"][-1]
        if last["role"] == "tool":
            return reply()
        if last["content"] == "B 的确认卡":
            return batch(
                ("set_current_job", {"job_id": str(resume_b.inputs.user_job_id)})
            )
        return batch(*crosses)

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        card = card_of(
            await user_b.post(f"{path_b}/messages", json=message_body("B 的确认卡"))
        )
        before_b = (await user_b.get(path_b)).json()
        product_b = (await user_b.get(resume_b.path)).json()
        for path in (path_b, path_a):
            blocked = await user_a.post(
                f"{path}/actions/{card['action_id']}", json={"decision": "confirm"}
            )
            assert blocked.status_code == 404
        assert (
            await user_a.post(f"/api/actions/{card['action_id']}/undo")
        ).status_code == 404

        result = await user_a.post(
            f"{path_a}/messages", json=message_body("尝试跨用户")
        )
        steps = [body for name, body in events(result) if name == "tool_end"]
        assert len(steps) == len(crosses)
        assert [step.get("status") for step in steps] == [404] * len(crosses)
        assert not any(name == "confirm" for name, _ in events(result))

    assert (await user_b.get(path_b)).json() == before_b
    assert (await user_b.get(resume_b.path)).json() == product_b
