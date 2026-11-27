"""模型服务连不上时，这一轮要说清原因和怎么办，而不是笼统的「未完成」。"""

import httpx

from jobpilot.api.deps import get_model_client
from jobpilot.main import app
from jobpilot.tests.test_agent_api import (
    conversation,
    done_of,
    events,
    message_body,
    sign_in,
)
from jobpilot.tests.test_call_accounting import fake_upstream


async def test_connection_timeout_tells_user_to_check_network_and_resend(
    async_client: httpx.AsyncClient, monkeypatch
) -> None:
    from jobpilot.config import settings

    monkeypatch.setattr(settings, "model_retry_delay_seconds", 0)
    await sign_in(async_client)
    path = await conversation(async_client)
    attempts = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectTimeout("模拟连接超时", request=request)

    async with fake_upstream(respond) as fake:
        app.dependency_overrides[get_model_client] = lambda: fake
        result = await async_client.post(f"{path}/messages", json=message_body("你好"))

    assert attempts == settings.model_max_attempts
    assert done_of(result)["status"] == "failed"
    errors = [body for name, body in events(result) if name == "error"]
    assert errors and "连不上模型服务" in errors[0]["message"]
    saved = (await async_client.get(path)).json()["messages"]
    assert "检查网络" in saved[-1]["content"]
