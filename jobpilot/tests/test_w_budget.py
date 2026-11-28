"""确认评测费用在发请求前预留，结算使用供应商 usage。"""

import json
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

import httpx
import pytest
from evals.runners import w_budget


class _SseUsage(httpx.AsyncByteStream):
    chunks: ClassVar[tuple[bytes, ...]] = (
        b'data: {"usage":{"prompt_tokens":',
        b'12,"completion_tokens":7}}\n\n',
        b"data: [DONE]\n\n",
    )

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


async def test_w_cap_stops_before_sending_and_uses_stream_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        w_budget, "frozen_git_state", lambda: {"commit": "fixed", "dirty": False}
    )
    path = tmp_path / "billing.json"
    w_budget.init(path, Decimal("0.02"), Decimal("0.01"))
    budget = w_budget.BatchBudget(path)
    calls = []

    def send(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200, headers={"Content-Type": "text/event-stream"}, stream=_SseUsage()
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(send),
        event_hooks={"request": [budget.request], "response": [budget.response]},
    ) as client:
        payload = {"model": "deepseek-flash", "stream": True, "max_tokens": 10}
        result = await client.post("https://synthetic.invalid/v1", json=payload)
        assert result.content == b"".join(_SseUsage.chunks)
        assert budget.record["known_cost"] == 12 * 2 + 7 * 8
        assert budget.record["reserved_cost"] == 0
        with pytest.raises(w_budget.BatchLimitReached):
            await client.post(
                "https://synthetic.invalid/v1", json={**payload, "max_tokens": 2000}
            )
    assert len(calls) == 1
    assert json.loads(path.read_text())["stopped"] == "next_request_exceeds_cap"
