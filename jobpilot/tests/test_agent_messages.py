"""通过消息接口验证网络重试与竞争提交只执行已接受的消息。"""

import asyncio
from uuid import uuid4

import httpx

from jobpilot.api.deps import get_model_client
from jobpilot.main import app
from jobpilot.tests.test_agent_api import conversation, done_of, events, reply, sign_in
from jobpilot.tests.test_call_accounting import fake_upstream


async def test_same_message_id_replays_finished_turn(
    async_client: httpx.AsyncClient,
) -> None:
    await sign_in(async_client)
    path = await conversation(async_client)
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return reply("持久化答复")

    payload = {"client_message_id": str(uuid4()), "content": "第一条消息"}
    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        first = await async_client.post(f"{path}/messages", json=payload)
        retry = await async_client.post(
            f"{path}/messages", json={**payload, "content": "重试的内容不覆盖原消息"}
        )
    assert done_of(first) == done_of(retry)
    assert ("text", {"text": "持久化答复"}) in events(retry)
    assert len(requests) == 1
    messages = (await async_client.get(path)).json()["messages"]
    assert [message["content"] for message in messages] == ["第一条消息", "持久化答复"]


async def test_different_message_id_gets_conflict_while_turn_is_running(
    async_client: httpx.AsyncClient,
) -> None:
    await sign_in(async_client)
    path = await conversation(async_client)
    received = asyncio.Event()
    finish = asyncio.Event()

    async def respond(request: httpx.Request) -> httpx.Response:
        received.set()
        await finish.wait()
        return reply()

    payload = {"client_message_id": str(uuid4()), "content": "已接收"}
    async with fake_upstream(respond) as fake, asyncio.timeout(5):
        app.dependency_overrides[get_model_client] = lambda: fake
        first = asyncio.create_task(async_client.post(f"{path}/messages", json=payload))
        try:
            await received.wait()
            retry = asyncio.create_task(
                async_client.post(f"{path}/messages", json=payload)
            )
            active = (await async_client.get(path)).json()["run"]
            conflict = await async_client.post(
                f"{path}/messages",
                json={"client_message_id": str(uuid4()), "content": "未接收"},
            )
            assert conflict.status_code == 409
            assert conflict.json()["detail"] == {
                "message": "这条消息没有保存",
                "run_id": active["run_id"],
                "pending_action": None,
            }
        finally:
            finish.set()
            first_response = await first
        assert done_of(first_response) == done_of(await retry)
    assert len((await async_client.get(path)).json()["messages"]) == 2
