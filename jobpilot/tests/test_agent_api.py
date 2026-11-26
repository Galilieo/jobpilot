"""通过真实 HTTP、检查点和数据库验证确认流程，替换模型上游。"""

import asyncio
from collections.abc import Coroutine
from json import dumps, loads
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from jobpilot.api.deps import get_model_client
from jobpilot.config import settings
from jobpilot.db.models import AiRun, Conversation, ModelCall
from jobpilot.db.runs import fail_abandoned_runs
from jobpilot.main import app
from jobpilot.services.agent.runtime import AgentRuntime
from jobpilot.tests.test_agent_gateway import stream_response
from jobpilot.tests.test_auth import login, register
from jobpilot.tests.test_call_accounting import completion_response, fake_upstream
from jobpilot.tests.test_facts_api import new_idempotency_headers
from jobpilot.tests.test_jobs_api import JD, OUTPUT
from jobpilot.tests.test_tailoring_graph import Factory, FakeModel, seed_inputs


def events(response: httpx.Response) -> list[tuple[str, dict]]:
    assert response.status_code == 200, response.text
    return [
        (name.removeprefix("event: "), loads(data.removeprefix("data: ")))
        for block in response.text.strip().split("\n\n")
        if block
        for name, data in [block.splitlines()]
    ]


def tool_call(name: str, arguments: dict, call_id: str = "call-one") -> dict:
    return {
        "role": "assistant",
        "tool_calls": [
            {
                "index": 0,
                "type": "function",
                "id": call_id,
                "function": {"name": name, "arguments": dumps(arguments)},
            }
        ],
    }


async def sign_in(
    client: httpx.AsyncClient, email: str = "agent@example.invalid"
) -> None:
    assert (await register(client, email)).is_success
    assert (await login(client, email)).is_success


async def conversation(client: httpx.AsyncClient) -> str:
    response = await client.post("/api/conversations", json={})
    assert response.status_code == 201, response.text
    return f"/api/conversations/{response.json()['conversation_id']}"


def message_body(content: str = "", **fields: object) -> dict:
    return {"client_message_id": str(uuid4()), "content": content, **fields}


async def test_cancelled_account_blocks_agent_launch_after_message_commit(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """删号先于任务登记完成，之后启动的任务不会向模型发请求。"""
    await sign_in(async_client)
    path = await conversation(async_client)
    launcher = app.state.run_launcher
    pending: asyncio.Future[tuple[UUID, Coroutine[Any, Any, None]]] = (
        asyncio.get_running_loop().create_future()
    )
    launch = launcher.launch

    def delay_launch(run_id: UUID, work: Coroutine[Any, Any, None]) -> None:
        pending.set_result((run_id, work))

    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return reply()

    monkeypatch.setattr(launcher, "launch", delay_launch)
    async with fake_upstream(respond) as fake, asyncio.timeout(5):
        app.dependency_overrides[get_model_client] = lambda: fake
        sending = asyncio.create_task(
            async_client.post(f"{path}/messages", json=message_body("启动对话"))
        )
        run_id, work = await pending
        try:
            assert (await async_client.delete("/api/account")).status_code == 204
        finally:
            launch(run_id, work)
            await sending
            await launcher.wait_all()

    assert requests == []
    async with session_factory() as reader:
        assert await reader.scalar(
            select(AiRun.status).where(AiRun.run_id == run_id)
        ) == ("cancelled")


async def test_job_change_waits_then_runs_once_and_can_be_undone(
    async_client: httpx.AsyncClient,
    model_outputs: list[str],
) -> None:
    await sign_in(async_client)
    model_outputs.append(dumps(OUTPUT))
    job = await async_client.post(
        "/api/jobs",
        json={"raw_text": JD},
        headers=new_idempotency_headers(),
    )
    assert job.status_code == 201, job.text
    job_id = job.json()["job_id"]
    path = await conversation(async_client)

    def respond(request: httpx.Request) -> httpx.Response:
        payload = loads(request.content)
        if payload["messages"][-1]["role"] == "tool":
            return stream_response({"role": "assistant", "content": "已经切换。"})
        return stream_response(
            tool_call("set_current_job", {"job_id": job_id}), finish_reason="tool_calls"
        )

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        sent = events(
            await async_client.post(path + "/messages", json=message_body("切换岗位"))
        )
        card = next(body for name, body in sent if name == "confirm")
        assert (await async_client.get(path)).json()["current_job_id"] is None

        action_path = f"{path}/actions/{card['action_id']}"
        decided = events(
            await async_client.post(action_path, json={"decision": "confirm"})
        )
        repeated = events(
            await async_client.post(action_path, json={"decision": "reject"})
        )
        done = next(body for name, body in decided if name == "done")
        assert (
            next(body for name, body in repeated if name == "done")["action"]
            == done["action"]
        )

    assert (await async_client.get(path)).json()["current_job_id"] == job_id
    assert (
        await async_client.post(f"/api/actions/{card['action_id']}/undo")
    ).status_code == 200
    assert (await async_client.get(path)).json()["current_job_id"] is None


async def test_undo_cannot_overwrite_a_later_binding_back_to_same_job(
    async_client: httpx.AsyncClient, session_factory: Factory
) -> None:
    await sign_in(async_client)
    owner = UUID((await async_client.get("/api/me")).json()["user_id"])
    job_id = str((await seed_inputs(session_factory, owner)).user_job_id)
    path = await conversation(async_client)

    def respond(request: httpx.Request) -> httpx.Response:
        if loads(request.content)["messages"][-1]["role"] == "tool":
            return reply()
        return batch(("set_current_job", {"job_id": job_id}))

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        card = card_of(
            await async_client.post(f"{path}/messages", json=message_body("绑定岗位"))
        )
        applied = await async_client.post(
            f"{path}/actions/{card['action_id']}", json={"decision": "confirm"}
        )
    assert done_of(applied)["action"]["status"] == "executed"
    assert (await async_client.put(path, json={"current_job_id": None})).is_success
    assert (await async_client.put(path, json={"current_job_id": job_id})).is_success
    undo = await async_client.post(f"/api/actions/{card['action_id']}/undo")
    assert undo.status_code == 409
    assert (await async_client.get(path)).json()["current_job_id"] == job_id


async def test_deleted_job_advances_binding_revision_when_unlinked(
    async_client: httpx.AsyncClient, session_factory: Factory
) -> None:
    await sign_in(async_client)
    owner = UUID((await async_client.get("/api/me")).json()["user_id"])
    job_id = str((await seed_inputs(session_factory, owner)).user_job_id)
    path = await conversation(async_client)
    assert (await async_client.put(path, json={"current_job_id": job_id})).is_success
    conversation_id = UUID(path.rsplit("/", 1)[-1])
    async with session_factory() as reader:
        bound = await reader.get_one(Conversation, conversation_id)
    assert (await async_client.delete(f"/api/jobs/{job_id}")).status_code == 204
    async with session_factory() as reader:
        unbound = await reader.get_one(Conversation, conversation_id)
    assert unbound.current_job_id is None
    assert unbound.job_binding_revision == bound.job_binding_revision + 1


def batch(*tool_calls: tuple[str, dict]) -> httpx.Response:
    items = [
        tool_call(name, args, f"call-{index}")["tool_calls"][0] | {"index": index}
        for index, (name, args) in enumerate(tool_calls)
    ]
    return stream_response(
        {"role": "assistant", "tool_calls": items}, finish_reason="tool_calls"
    )


def card_of(response: httpx.Response) -> dict:
    return next(body for name, body in events(response) if name == "confirm")


def done_of(response: httpx.Response) -> dict:
    return next(body for name, body in events(response) if name == "done")


def reply(text: str = "已处理") -> httpx.Response:
    return stream_response({"role": "assistant", "content": text})


async def _restart_runtime(sessions: Factory, monkeypatch: pytest.MonkeyPatch) -> None:
    await app.state.run_launcher.wait_all()
    async with sessions.begin() as session:
        await fail_abandoned_runs(session)
    monkeypatch.setitem(app.state._state, "agent_runtime", AgentRuntime())


async def test_restart_restores_one_card_without_resetting_budget(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await sign_in(async_client)
    user_id = UUID((await async_client.get("/api/me")).json()["user_id"])
    inputs = await seed_inputs(session_factory, user_id)
    path = await conversation(async_client)
    monkeypatch.setattr(settings.agent_limits, "model_calls", 1)
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return batch(("set_current_job", {"job_id": str(inputs.user_job_id)}))

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        card = card_of(
            await async_client.post(f"{path}/messages", json=message_body("绑定岗位"))
        )
        await _restart_runtime(session_factory, monkeypatch)
        recovered = (await async_client.get(path)).json()
        assert recovered["pending_action"] == card
        assert recovered["confirmation_status"] == "waiting_decision"
        again = await async_client.post(
            f"{path}/messages", json=message_body("别开新运行")
        )
        assert again.status_code == 409
        assert again.json()["detail"]["pending_action"] == card
        action_url = f"{path}/actions/{card['action_id']}"
        first, second = await asyncio.gather(
            *[
                async_client.post(action_url, json={"decision": "confirm"})
                for _ in range(2)
            ]
        )

    assert done_of(first) == done_of(second)
    assert done_of(first)["status"] == "budget_exhausted"
    assert len(requests) == 1
    recovered = (await async_client.get(path)).json()
    assert recovered["current_job_id"] == str(inputs.user_job_id)
    assert sum(message["role"] == "user" for message in recovered["messages"]) == 1


async def test_two_writes_are_serial_and_rejection_does_not_write(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await sign_in(async_client)
    user_id = UUID((await async_client.get("/api/me")).json()["user_id"])
    inputs = await seed_inputs(session_factory, user_id)
    job_id = str(inputs.user_job_id)
    path = await conversation(async_client)

    def respond(request: httpx.Request) -> httpx.Response:
        if loads(request.content)["messages"][-1]["role"] == "tool":
            return reply()
        return batch(
            ("set_current_job", {"job_id": job_id}),
            ("set_current_job", {"job_id": None}),
        )

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        first = card_of(
            await async_client.post(
                f"{path}/messages", json=message_body("测试两个写入")
            )
        )
        await _restart_runtime(session_factory, monkeypatch)
        first_url = f"{path}/actions/{first['action_id']}"
        declined = await async_client.post(first_url, json={"decision": "reject"})
        second = card_of(declined)
        assert first["action_id"] != second["action_id"]
        assert done_of(declined)["action"]["status"] == "rejected"
        assert (await async_client.get(path)).json()["current_job_id"] is None
        approved = await async_client.post(
            f"{path}/actions/{second['action_id']}", json={"decision": "confirm"}
        )
        assert done_of(approved)["action"]["status"] == "executed"
        replay = await async_client.post(first_url, json={"decision": "confirm"})
        assert done_of(replay)["action"] == done_of(declined)["action"]
    assert (await async_client.get(path)).json()["current_job_id"] is None


async def test_restart_counts_model_request_without_checkpoint(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """恢复时计入已经发出、结果未知、检查点还没前进的调用。"""
    await sign_in(async_client)
    path = await conversation(async_client)
    monkeypatch.setattr(settings.agent_limits, "model_calls", 2)
    requests = []
    received = asyncio.Event()
    respond_to_interrupted_request = asyncio.Event()

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return batch(("set_current_job", {"job_id": None}))
        if len(requests) == 2:
            received.set()
            await respond_to_interrupted_request.wait()
        return reply()

    async with fake_upstream(respond) as fake, asyncio.timeout(5):
        app.dependency_overrides[get_model_client] = lambda: fake
        card = card_of(
            await async_client.post(
                f"{path}/messages", json=message_body("解除岗位绑定")
            )
        )
        action_url = f"{path}/actions/{card['action_id']}"
        deciding = asyncio.create_task(
            async_client.post(action_url, json={"decision": "confirm"})
        )
        try:
            await received.wait()
            app.state.run_launcher.cancel_runs([UUID(card["run_id"])])
            await deciding
        finally:
            respond_to_interrupted_request.set()
        assert len(requests) == 2
        await _restart_runtime(session_factory, monkeypatch)
        resumed = await async_client.post(action_url, json={"decision": "confirm"})

    assert len(requests) == 2
    assert done_of(resumed)["status"] == "budget_exhausted"


async def test_questions_merge_and_reply_is_sent_as_untrusted_tool_result(
    async_client: httpx.AsyncClient,
) -> None:
    await sign_in(async_client)
    path = await conversation(async_client)
    messages = []

    def respond(request: httpx.Request) -> httpx.Response:
        turn = loads(request.content)["messages"]
        messages.append(turn)
        if turn[-1]["role"] == "tool":
            return reply()
        return batch(
            ("ask_user", {"questions": ["何时到岗？"]}),
            ("ask_user", {"questions": ["每周几天？"]}),
        )

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        card = card_of(
            await async_client.post(
                f"{path}/messages", json=message_body("帮我补全求职条件")
            )
        )
        assert card["kind"] == "question"
        invalid = await async_client.post(
            f"{path}/actions/{card['action_id']}",
            json={"decision": "confirm", "answers": ["只有一个回答"]},
        )
        assert invalid.status_code == 422
        assert card["args"]["questions"] == [
            {"question_id": "q1", "text": "何时到岗？"},
            {"question_id": "q2", "text": "每周几天？"},
        ]
        missing_answer = await async_client.post(
            f"{path}/actions/{card['action_id']}",
            json={"decision": "confirm", "answers": {"q1": "下个月"}},
        )
        assert missing_answer.status_code == 422
        decision = await async_client.post(
            f"{path}/actions/{card['action_id']}",
            json={"decision": "confirm", "answers": {"q2": "每周四天", "q1": "下个月"}},
        )
    assert done_of(decision)["status"] == "succeeded"
    tool_results = messages[-1][-2:]
    assert all(item["role"] == "tool" for item in tool_results)
    assert all(
        'label="tool_result"' in item["content"] and "每周四天" in item["content"]
        for item in tool_results
    )
    assert done_of(decision)["action"]["result"]["answers"]["q1"] == {
        "question": "何时到岗？",
        "answer": "下个月",
    }


async def test_tailoring_child_cannot_send_second_request_on_shared_budget(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await sign_in(async_client)
    user_id = UUID((await async_client.get("/api/me")).json()["user_id"])
    inputs = await seed_inputs(session_factory, user_id)
    path = await conversation(async_client)
    monkeypatch.setattr(settings.agent_limits, "model_calls", 2)
    downstream = FakeModel(inputs.fact_ids)
    purposes = []

    def respond(request: httpx.Request) -> httpx.Response:
        if loads(request.content).get("stream"):
            purposes.append("agent")
            return batch(("tailor_resume", {"job_id": str(inputs.user_job_id)}))
        purposes.append("child")
        return downstream(request)

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        result = await async_client.post(
            f"{path}/messages", json=message_body("定制简历")
        )
    assert done_of(result)["status"] == "budget_exhausted"
    assert purposes == ["agent", "child"]
    assert downstream.calls == {"match": 1, "rewrite": 0, "check": 0}
    step = next(body for name, body in events(result) if name == "tool_end")
    recorded = (await async_client.get(f"/api/tailor-runs/{step['run_id']}")).json()
    assert recorded["status"] == "budget_exhausted"
    assert recorded["base_doc_id"] == str(inputs.doc_id)
    assert recorded["blocks"] and all(
        block["status"] != "done" for block in recorded["blocks"]
    )


@pytest.mark.parametrize("choice", ["keep", "new", "switch"])
async def test_parsing_a_second_job_waits_for_binding_choice(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    choice: str,
) -> None:
    await sign_in(async_client)
    user_id = UUID((await async_client.get("/api/me")).json()["user_id"])
    first = await seed_inputs(session_factory, user_id)
    path = await conversation(async_client)
    old_job = str(first.user_job_id)
    assert (
        await async_client.put(path, json={"current_job_id": old_job})
    ).status_code == 200

    def respond(request: httpx.Request) -> httpx.Response:
        payload = loads(request.content)
        if not payload.get("stream"):
            return completion_response(dumps(OUTPUT))
        if payload["messages"][-1]["role"] == "tool":
            return reply()
        return batch(("parse_jd", {"source": 1}))

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        card = card_of(
            await async_client.post(
                f"{path}/messages",
                json=message_body(attachments=[{"kind": "jd", "text": JD}]),
            )
        )
        assert card["args"]["type"] == "job_choice"
        assert card["args"]["choices"] == ["new", "switch", "keep"]
        assert (await async_client.get(path)).json()["current_job_id"] == old_job
        invalid = await async_client.post(
            f"{path}/actions/{card['action_id']}",
            json={"decision": "confirm", "answers": {"q1": "切换"}},
        )
        assert invalid.status_code == 422
        decided = await async_client.post(
            f"{path}/actions/{card['action_id']}",
            json={"decision": "confirm", "job_choice": choice},
        )
        new_job = card["args"]["job_id"]
        expected = new_job if choice == "switch" else old_job
        assert (await async_client.get(path)).json()["current_job_id"] == expected
        if choice == "new":
            new_id = done_of(decided)["action"]["result"]["conversation_id"]
            created = (await async_client.get(f"/api/conversations/{new_id}")).json()
            assert created["current_job_id"] == new_job


async def test_parse_jd_uses_full_input_without_sending_contact_details(
    async_client: httpx.AsyncClient,
) -> None:
    await sign_in(async_client)
    path = await conversation(async_client)
    email = "recruiting@example.invalid"
    full_jd = f"{JD}\n招聘邮箱 {email}\n其余岗位原文"
    sent_payloads: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent_payloads.append(request.content.decode())
        payload = loads(request.content)
        if not payload.get("stream"):
            return completion_response(dumps(OUTPUT))
        if payload["messages"][-1]["role"] == "tool":
            return reply()
        return batch(("parse_jd", {"source": 1}))

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        response = await async_client.post(
            f"{path}/messages",
            json=message_body(
                "分析附件", attachments=[{"text": full_jd, "kind": "jd"}]
            ),
        )
    result = next(body for name, body in events(response) if name == "tool_end")
    assert "job_id" in result
    saved = await async_client.get(f"/api/jobs/{result['job_id']}")
    assert saved.json()["raw_text"] == full_jd
    assert len(sent_payloads) == 3 and all(email not in body for body in sent_payloads)
    assert "[EMAIL]" in sent_payloads[1]


async def test_bad_tool_json_does_not_fail_other_tools_or_model_accounting(
    async_client: httpx.AsyncClient, session_factory: Factory
) -> None:
    await sign_in(async_client)
    path = await conversation(async_client)

    def respond(request: httpx.Request) -> httpx.Response:
        if loads(request.content)["messages"][-1]["role"] == "tool":
            return reply()
        invalid = tool_call("list_jobs", {}, "invalid")["tool_calls"][0]
        invalid["function"]["arguments"] = "{unfinished"
        valid = tool_call("list_jobs", {}, "valid")["tool_calls"][0] | {"index": 1}
        return stream_response(
            {"role": "assistant", "tool_calls": [invalid, valid]},
            finish_reason="tool_calls",
        )

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        response = await async_client.post(
            f"{path}/messages", json=message_body("列岗位")
        )
    steps = {
        body["tool_call_id"]: body
        for name, body in events(response)
        if name == "tool_end"
    }
    assert steps["invalid"]["status"] == 422
    assert steps["valid"]["total"] == 0
    async with session_factory() as reader:
        assert list(await reader.scalars(select(ModelCall.outcome))) == [
            "success",
            "success",
        ]


async def test_prompt_uses_recent_turns_and_keeps_database_history(
    async_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await sign_in(async_client)
    path = await conversation(async_client)
    captured = []

    def respond(request: httpx.Request) -> httpx.Response:
        captured.append(loads(request.content)["messages"])
        return reply("答复")

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        for index in range(12):
            result = await async_client.post(
                f"{path}/messages", json=message_body(f"第 {index} 轮")
            )
            assert done_of(result)["status"] == "succeeded"
        assert [msg["content"] for msg in captured[-1] if msg["role"] == "user"] == [
            f"第 {index} 轮" for index in range(2, 12)
        ]
        monkeypatch.setattr(settings.agent_limits, "history_tokens", 1)
        result = await async_client.post(
            f"{path}/messages", json=message_body("保留本轮")
        )
        assert done_of(result)["status"] == "succeeded"
    assert [msg["content"] for msg in captured[-1] if msg["role"] == "user"] == [
        "保留本轮"
    ]
    assert len((await async_client.get(path)).json()["messages"]) == 26
