"""对话上下文用量：和自动压缩用同一套估算，随对话增长，按用户隔离。"""

import httpx

from jobpilot.api.deps import get_model_client
from jobpilot.config import ModelPurpose, settings
from jobpilot.main import app
from jobpilot.tests.test_agent_api import (
    conversation,
    done_of,
    message_body,
    reply,
    sign_in,
)
from jobpilot.tests.test_call_accounting import fake_upstream


async def test_context_usage_grows_with_turns_and_reports_compact_line(
    async_client: httpx.AsyncClient,
) -> None:
    await sign_in(async_client)
    path = await conversation(async_client)

    empty = await async_client.get(f"{path}/context")
    assert empty.status_code == 200, empty.text
    window = settings.effective_window(ModelPurpose.AGENT)
    output = min(
        settings.model_for_purpose(ModelPurpose.AGENT).max_tokens,
        settings.model_profile(ModelPurpose.AGENT).max_output_tokens,
    )
    assert empty.json()["window_tokens"] == window
    assert empty.json()["compact_at_tokens"] == (
        int(window * settings.agent_limits.compact_trigger_ratio) - output
    )
    # 新对话也有系统提示词和工具定义。
    assert empty.json()["used_tokens"] > 0

    async with fake_upstream(lambda _: reply("好的")) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        result = await async_client.post(
            f"{path}/messages", json=message_body("甲" * 2000)
        )
        assert done_of(result)["status"] == "succeeded"

    grown = await async_client.get(f"{path}/context")
    assert grown.json()["used_tokens"] > empty.json()["used_tokens"] + 1000


async def test_context_usage_is_isolated_per_user(
    async_client: httpx.AsyncClient,
) -> None:
    await sign_in(async_client)
    path = await conversation(async_client)
    await async_client.post("/api/auth/logout")
    await sign_in(async_client, "other@example.invalid")

    assert (await async_client.get(f"{path}/context")).status_code == 404
