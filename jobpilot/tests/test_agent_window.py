"""验证 Agent 在上游报超长时只重发更小的请求，并保留数据库历史。"""

from json import dumps, loads
from uuid import UUID

import httpx
import pytest

from jobpilot.api.deps import get_model_client
from jobpilot.config import settings
from jobpilot.db.models import Conversation
from jobpilot.main import app
from jobpilot.tests.test_agent_api import (
    conversation,
    done_of,
    events,
    message_body,
    reply,
    sign_in,
)
from jobpilot.tests.test_call_accounting import fake_upstream
from jobpilot.tests.test_tailoring_graph import Factory


@pytest.mark.parametrize("overflow", [True, False])
async def test_upstream_overflow_is_retried_once_with_less_context(
    async_client: httpx.AsyncClient,
    session_factory: Factory,
    overflow: bool,
) -> None:
    await sign_in(async_client)
    path = await conversation(async_client)
    attempts = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = loads(request.content)
        if payload["messages"][-1]["content"] != "新问题":
            return reply()
        attempts.append(payload)
        if len(attempts) == 1:
            message = (
                "Maximum context length exceeded" if overflow else "invalid arguments"
            )
            return httpx.Response(400, json={"error": {"message": message}})
        return reply()

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        first = await async_client.post(
            f"{path}/messages", json=message_body("甲" * 3600)
        )
        assert done_of(first)["status"] == "succeeded"
        response = await async_client.post(
            f"{path}/messages", json=message_body("新问题")
        )

    assert len(attempts) == (2 if overflow else 1)
    assert done_of(response)["status"] == ("succeeded" if overflow else "failed")
    if overflow:
        assert len(dumps(attempts[-1], ensure_ascii=False).encode()) < len(
            dumps(attempts[0], ensure_ascii=False).encode()
        )
        assert "read_conversation_history" in attempts[-1]["messages"][0]["content"]
        assert "本次未包含" in attempts[-1]["messages"][0]["content"]
    async with session_factory() as db:
        saved = await db.get_one(Conversation, UUID(path.rsplit("/", 1)[-1]))
    assert saved.summary_through_seq == 0
    assert len((await async_client.get(path)).json()["messages"]) == 4


async def test_current_input_overflow_has_explicit_split_feedback(
    async_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await sign_in(async_client)
    path = await conversation(async_client)
    monkeypatch.setattr(settings.agent_limits, "context_window_cap_tokens", 9000)

    def unexpected(request: httpx.Request) -> httpx.Response:
        raise AssertionError("超出窗口的正文不能发送给模型")

    async with fake_upstream(unexpected) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        response = await async_client.post(
            f"{path}/messages", json=message_body("甲" * 8500)
        )
    assert done_of(response)["status"] == "failed"
    assert any(
        "请拆分" in item.get("message", "")
        for name, item in events(response)
        if name == "error"
    )


async def test_streamed_text_is_not_emitted_twice_on_overflow(
    async_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jobpilot.services import llm_gateway
    from jobpilot.services.llm_gateway import ContextOverflowError

    await sign_in(async_client)
    path = await conversation(async_client)
    calls = 0

    async def fail_after_text(completions, arguments, on_text):
        nonlocal calls
        calls += 1
        on_text("已经发送的片段")
        request = httpx.Request("POST", "https://fake-model.invalid")
        rejected = httpx.Response(400, request=request)
        raise ContextOverflowError("超长", response=rejected, body=None)

    async with fake_upstream(lambda _: reply()) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        seeded = await async_client.post(
            f"{path}/messages", json=message_body("甲" * 3000)
        )
        assert seeded.is_success
        monkeypatch.setattr(llm_gateway, "_read_completion_stream", fail_after_text)
        response = await async_client.post(
            f"{path}/messages", json=message_body("继续")
        )
    assert calls == 1
    assert [item["text"] for event, item in events(response) if event == "text"] == [
        "已经发送的片段"
    ]
    assert done_of(response)["status"] == "failed"
