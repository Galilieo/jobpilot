"""验证工具次数上限只拦失控的运行，用完时让模型用已拿到的信息作答。"""

from json import loads

import httpx
import pytest

from jobpilot.api.deps import get_model_client
from jobpilot.config import settings
from jobpilot.main import app
from jobpilot.tests.test_agent_api import (
    batch,
    conversation,
    done_of,
    events,
    message_body,
    reply,
    sign_in,
)
from jobpilot.tests.test_call_accounting import fake_upstream


def _tool_results(request: httpx.Request) -> list[dict]:
    return [
        message
        for message in loads(request.content)["messages"]
        if message["role"] == "tool"
    ]


def _payload(message: dict) -> dict:
    # 工具结果发给模型前包在不可信数据标记里，正文在首尾两行之间。
    content = message["content"]
    return loads(content[content.index("\n") + 1 : content.rindex("\n")])


def _texts(response: httpx.Response) -> list[str]:
    return [body["text"] for name, body in events(response) if name == "text"]


async def test_default_limit_allows_a_long_search_and_read_turn(
    async_client: httpx.AsyncClient,
) -> None:
    # 搜一次再翻几段旧对话就要十几次读取，默认上限不能把它当成失控。
    await sign_in(async_client)
    path = await conversation(async_client)

    def respond(request: httpx.Request) -> httpx.Response:
        if len(_tool_results(request)) < 13:
            return batch(("list_jobs", {}))
        return reply("读完了")

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        result = await async_client.post(
            f"{path}/messages", json=message_body("帮我翻一下")
        )
    assert done_of(result)["status"] == "succeeded"


async def test_exhausted_tools_answer_from_gathered_results(
    async_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await sign_in(async_client)
    path = await conversation(async_client)
    monkeypatch.setattr(settings.agent_limits, "tool_calls", 1)
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if not _tool_results(request):
            return batch(("list_jobs", {}), ("list_jobs", {}))
        return reply("按已经查到的回答")

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        result = await async_client.post(
            f"{path}/messages", json=message_body("列两次岗位")
        )

    assert len(requests) == 2
    executed, skipped = _tool_results(requests[1])
    assert _payload(executed)["total"] == 0
    assert _payload(skipped)["status"] == "tool_limit_reached"
    assert done_of(result)["status"] == "budget_exhausted"
    assert _texts(result) == ["按已经查到的回答"]
    messages = (await async_client.get(path)).json()["messages"]
    assert messages[-1]["content"] == "按已经查到的回答"


async def test_tool_calls_after_the_limit_stop_with_the_notice(
    async_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await sign_in(async_client)
    path = await conversation(async_client)
    monkeypatch.setattr(settings.agent_limits, "tool_calls", 1)
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return batch(("list_jobs", {}))

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        result = await async_client.post(
            f"{path}/messages", json=message_body("一直查")
        )

    # 第二次请求越过上限后再给一次不带工具作答的机会，第三次仍调工具才停。
    assert len(requests) == 3
    assert _payload(_tool_results(requests[2])[-1])["status"] == "tool_limit_reached"
    assert done_of(result)["status"] == "budget_exhausted"
    assert _texts(result) == [
        "本轮额度已用完。已完成的步骤保留在对话中，可以发消息继续。"
    ]
