"""按任务 W 的非缓存单价预留每次请求成本，并用实际用量结算。"""

import argparse
import json
import os
import subprocess
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from decimal import Decimal
from fcntl import LOCK_EX, flock
from pathlib import Path

import httpx

from evals.runners.e1_extract_facts import git_state
from evals.runners.e3_match_io import write_record
from jobpilot.config import REPO_ROOT

BUDGET_ENV = "JOBPILOT_W_BUDGET"
RATES = {"deepseek-flash": (2, 8), "deepseek-v4-pro": (9, 27)}
MICRO_YUAN_SCALE = 1_000_000  # 使用百万分之一元，便于按整数记账。
CHAT_OVERHEAD = 1024


def frozen_git_state() -> dict:
    result = git_state()
    eval_changes = subprocess.check_output(
        [
            "git",
            "status",
            "--porcelain",
            "--",
            "evals/runners",
            "evals/prompts",
            "evals/datasets",
            "evals/benchmark",
        ],
        cwd=REPO_ROOT,
        text=True,
    )
    return {**result, "dirty": result["dirty"] or bool(eval_changes.strip())}


class BatchLimitReached(ValueError):
    pass


class BatchBudget:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.record = json.loads(path.read_text(encoding="utf-8"))
        state = frozen_git_state()
        if state["dirty"] or self.record["commit"] != state["commit"]:
            raise ValueError("付费批次必须运行在冻结且干净的提交上")
        self.check()

    def check(self) -> None:
        if self.record.get("stopped"):
            raise BatchLimitReached("批次已停止，检查预算记录后再决定")

    async def request(self, request: httpx.Request) -> None:
        request.headers["Accept-Encoding"] = "identity"
        payload = json.loads(request.content)
        model = payload["model"]
        price_in, price_out = RATES[model]
        # 字节数加固定模板余量作为输入预留，按请求输出上限预留输出。
        reserve = (len(request.content) + CHAT_OVERHEAD) * price_in + payload[
            "max_tokens"
        ] * price_out
        with self._locked_record():
            self.check()
            combined = self.record["known_cost"] + self.record["reserved_cost"]
            if combined + reserve > self.record["limit"]:
                self.record["stopped"] = "next_request_exceeds_cap"
                self._save()
                self.check()
            request.extensions["w_budget"] = (
                model,
                reserve,
                bool(payload.get("stream")),
            )
            self.record["reserved_cost"] += reserve
            self.record["calls"] += 1
            self._save()

    async def response(self, response: httpx.Response) -> None:
        details = response.request.extensions.get("w_budget")
        if response.is_success and details is not None:
            model, reserve, streaming = details
            response.stream = AccountedStream(
                response.stream, self, model, reserve, streaming
            )

    def settle(self, model: str, reserved: int, usage: dict) -> None:
        prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
        if not all(type(value) is int and value >= 0 for value in (prompt, completion)):
            return
        price_in, price_out = RATES[model]
        with self._locked_record():
            self.record["reserved_cost"] -= reserved
            self.record["known_cost"] += prompt * price_in + completion * price_out
            self.record["prompt_tokens"] += prompt
            self.record["completion_tokens"] += completion
            self.record["known_calls"] += 1
            if (
                self.record["known_cost"] + self.record["reserved_cost"]
                >= self.record["limit"]
            ):
                self.record["stopped"] = "usage_reached_cap"
            self._save()

    @contextmanager
    def _locked_record(self) -> Iterator[None]:
        with self.path.with_suffix(".lock").open("a") as lock:
            flock(lock, LOCK_EX)
            self.record = json.loads(self.path.read_text(encoding="utf-8"))
            yield

    def _save(self) -> None:
        write_record(self.path, self.record)


class AccountedStream(httpx.AsyncByteStream):
    """响应照常流入 SDK，只在收到真实 usage 时替换预留。"""

    def __init__(self, stream, budget, model, reserved, streaming) -> None:
        self.stream = stream
        self.budget = budget
        self.model = model
        self.reserved = reserved
        self.streaming = streaming
        self.settled = False

    def _inspect(self, data: bytes) -> None:
        if self.settled or data in (b"", b"[DONE]"):
            return
        try:
            payload = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            return
        if isinstance(payload, dict) and isinstance(
            usage := payload.get("usage"), dict
        ):
            self.budget.settle(self.model, self.reserved, usage)
            self.settled = True

    async def __aiter__(self) -> AsyncIterator[bytes]:
        pending = b""
        async for chunk in self.stream:
            pending += chunk
            if self.streaming:
                lines = pending.split(b"\n")
                pending = lines.pop()
                for line in lines:
                    if line.startswith(b"data:"):
                        self._inspect(line[5:].strip())
            yield chunk
        if self.streaming and pending.startswith(b"data:"):
            self._inspect(pending[5:].strip())
        elif not self.streaming:
            self._inspect(pending)

    async def aclose(self) -> None:
        await self.stream.aclose()


def evaluation_http_client(
    request_hook=None, response_hook=None
) -> tuple[httpx.AsyncClient, BatchBudget | None]:
    path = os.environ.get(BUDGET_ENV)
    budget = BatchBudget(Path(path)) if path else None
    requests = ([budget.request] if budget else []) + (
        [request_hook] if request_hook else []
    )
    responses = ([budget.response] if budget else []) + (
        [response_hook] if response_hook else []
    )
    return httpx.AsyncClient(
        event_hooks={"request": requests, "response": responses}
    ), budget


def init(path: Path, approved: Decimal, expected: Decimal) -> None:
    if approved <= 0 or expected <= 0:
        raise ValueError("批准和预估金额都必须为正")
    state = frozen_git_state()
    if state["dirty"]:
        raise ValueError("先提交并冻结评测代码")
    cap = min(approved, expected * Decimal("1.2"))
    with path.open("x", encoding="utf-8") as stream:
        json.dump(
            {
                "commit": state["commit"],
                "limit": int(cap * MICRO_YUAN_SCALE),
                "known_cost": 0,
                "reserved_cost": 0,
                "calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "known_calls": 0,
            },
            stream,
            indent=2,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--approved", type=Decimal, required=True)
    parser.add_argument("--estimated", type=Decimal, required=True)
    args = parser.parse_args()
    init(args.path, args.approved, args.estimated)
