"""用真实检查点和同步点验证确认恢复及对话详情一致性。"""

import asyncio
from contextlib import AsyncExitStack
from json import loads
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI

from jobpilot import main
from jobpilot.api import agent as agent_api
from jobpilot.api import conversations as conversation_api
from jobpilot.api.deps import get_model_client
from jobpilot.tests.test_agent_api import (
    batch,
    card_of,
    conversation,
    done_of,
    message_body,
    reply,
    sign_in,
)
from jobpilot.tests.test_call_accounting import fake_upstream
from jobpilot.tests.test_tailoring_graph import Factory, seed_inputs


async def test_startup_resumes_a_saved_decision(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await sign_in(async_client)
    owner = UUID((await async_client.get("/api/me")).json()["user_id"])
    inputs = await seed_inputs(session_factory, owner)
    job_id = str(inputs.user_job_id)
    path = await conversation(async_client)
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if loads(request.content)["messages"][-1]["role"] == "tool":
            return reply("岗位已绑定")
        return batch(("set_current_job", {"job_id": job_id}))

    async with fake_upstream(respond) as fake:
        main.app.dependency_overrides[get_model_client] = lambda: fake
        card = card_of(
            await async_client.post(f"{path}/messages", json=message_body("绑定岗位"))
        )
        await main.app.state.run_launcher.wait_all()
        # 接口提交决定后、登记后台任务前退出。
        with monkeypatch.context() as crash:
            crash.setattr(
                agent_api, "_launch", lambda *args, **kwargs: agent_api._replay(args[4])
            )
            decision = await async_client.post(
                f"{path}/actions/{card['action_id']}", json={"decision": "confirm"}
            )
        assert decision.is_success
        saved = (await async_client.get(path)).json()
        assert saved["current_job_id"] is None
        assert saved["confirmation_status"] == "decision_saved"
        assert saved["latest_decided_action"]["status"] == "confirmed"

        restarted = FastAPI()
        monkeypatch.setattr(
            main,
            "create_database_engine",
            lambda *_a, **_kw: session_factory.kw["bind"],
        )
        monkeypatch.setattr(main, "AsyncOpenAI", lambda **_kw: fake)
        async with AsyncExitStack() as resources:
            await main._start_resources(restarted, resources)
            await restarted.state.run_launcher.wait_all()

    recovered = (await async_client.get(path)).json()
    assert recovered["run"]["status"] == "succeeded"
    assert recovered["current_job_id"] == job_id
    assert recovered["latest_decided_action"]["status"] == "executed"
    assert [msg["content"] for msg in recovered["messages"]].count("岗位已绑定") == 1
    assert len(requests) == 2


async def test_detail_keeps_messages_and_run_in_the_same_snapshot(
    async_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await sign_in(async_client)
    path = await conversation(async_client)
    respond_now = asyncio.Event()
    messages_read = asyncio.Event()
    query_can_continue = asyncio.Event()
    received = asyncio.Event()
    read_messages = conversation_api.conversation_messages

    async def pause_after_read(*args):
        rows = await read_messages(*args)
        messages_read.set()
        await query_can_continue.wait()
        return rows

    async def respond(request: httpx.Request) -> httpx.Response:
        received.set()
        await respond_now.wait()
        return reply("最终答复")

    async with fake_upstream(respond) as fake, asyncio.timeout(5):
        main.app.dependency_overrides[get_model_client] = lambda: fake
        monkeypatch.setattr(conversation_api, "conversation_messages", pause_after_read)
        sending = asyncio.create_task(
            async_client.post(f"{path}/messages", json=message_body("开始"))
        )
        # 等到消息已提交，避免依赖任务调度顺序。
        await received.wait()
        reading = asyncio.create_task(async_client.get(path))
        try:
            await messages_read.wait()
            respond_now.set()
            await sending
        finally:
            query_can_continue.set()
        observed = (await reading).json()

    assert observed["run"]["status"] == "running"
    assert all(msg["content"] != "最终答复" for msg in observed["messages"])
    settled = (await async_client.get(path)).json()
    assert settled["run"]["status"] == "succeeded"
    assert settled["messages"][-1]["content"] == "最终答复"


async def test_old_card_retry_and_delete_cannot_interrupt_new_card_resume(
    async_client: httpx.AsyncClient, session_factory: Factory
) -> None:
    await sign_in(async_client)
    owner = UUID((await async_client.get("/api/me")).json()["user_id"])
    job_id = str((await seed_inputs(session_factory, owner)).user_job_id)
    path = await conversation(async_client)
    resumed = asyncio.Event()
    finish = asyncio.Event()
    requests = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return batch(
                ("set_current_job", {"job_id": None}),
                ("set_current_job", {"job_id": job_id}),
            )
        resumed.set()
        await finish.wait()
        return reply()

    async with fake_upstream(respond) as fake, asyncio.timeout(5):
        main.app.dependency_overrides[get_model_client] = lambda: fake
        first_card = card_of(
            await async_client.post(f"{path}/messages", json=message_body("绑定岗位"))
        )
        first_url = f"{path}/actions/{first_card['action_id']}"
        rejected = await async_client.post(first_url, json={"decision": "reject"})
        second_card = card_of(rejected)
        recovering = asyncio.create_task(
            async_client.post(
                f"{path}/actions/{second_card['action_id']}",
                json={"decision": "confirm"},
            )
        )
        try:
            await resumed.wait()
            retried = await async_client.post(first_url, json={"decision": "confirm"})
            assert done_of(retried)["action"] == done_of(rejected)["action"]
            assert (await async_client.delete(path)).status_code == 409
            detail = (await async_client.get(path)).json()
            assert detail["confirmation_status"] == "recovering"
            assert (
                detail["latest_decided_action"]["action_id"] == second_card["action_id"]
            )
            assert detail["current_job_id"] == job_id
        finally:
            finish.set()
            result = await recovering
    assert done_of(result)["status"] == "succeeded"
    assert len(requests) == 2


async def test_delete_first_serializes_with_concurrent_confirmation(
    async_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await sign_in(async_client)
    path = await conversation(async_client)
    have_lock = asyncio.Event()
    release = asyncio.Event()
    decision_received = asyncio.Event()
    clear_threads = conversation_api.delete_checkpoint_threads
    lock_conversation = agent_api._lock_conversation

    async def pause_delete(*args):
        have_lock.set()
        await release.wait()
        await clear_threads(*args)

    async def observe_decision(*args):
        decision_received.set()
        return await lock_conversation(*args)

    monkeypatch.setattr(conversation_api, "delete_checkpoint_threads", pause_delete)
    monkeypatch.setattr(agent_api, "_lock_conversation", observe_decision)
    async with (
        fake_upstream(lambda _: batch(("set_current_job", {"job_id": None}))) as fake,
        asyncio.timeout(5),
    ):
        main.app.dependency_overrides[get_model_client] = lambda: fake
        card = card_of(
            await async_client.post(f"{path}/messages", json=message_body("解除岗位"))
        )
        decision_received.clear()
        deleting = asyncio.create_task(async_client.delete(path))
        await have_lock.wait()
        confirming = asyncio.create_task(
            async_client.post(
                f"{path}/actions/{card['action_id']}", json={"decision": "confirm"}
            )
        )
        try:
            await decision_received.wait()
        finally:
            release.set()
        deleted, confirmed = await asyncio.gather(deleting, confirming)
    assert (deleted.status_code, confirmed.status_code) == (204, 404)
