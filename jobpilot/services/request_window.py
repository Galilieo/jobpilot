"""估算主调用的输入窗口，只有可验证的同一请求前缀复用实测用量。"""

from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from json import dumps


def _json_bytes(value: object) -> bytes:
    return dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def estimate_request_tokens(messages: Sequence[object], tools: Sequence[object]) -> int:
    """用实际发送结构的 UTF-8 字节数估算输入窗口，输出空间由调用者预留。"""
    return len(_json_bytes(list(messages))) + len(_json_bytes(list(tools)))


def _fingerprint(
    configuration: str, messages: Sequence[object], tools: Sequence[object]
) -> bytes:
    return sha256(_json_bytes([configuration, list(tools), list(messages)])).digest()


@dataclass(frozen=True)
class _SentPrefix:
    count: int
    hash: bytes
    input_tokens: int


class InputTokenHistory:
    """每次 Agent 执行单独持有，只在成功主调用后更新，不保存请求正文。"""

    def __init__(self) -> None:
        self._prefix: _SentPrefix | None = None

    def estimate(
        self, configuration: str, messages: Sequence[object], tools: Sequence[object]
    ) -> int:
        previous = self._prefix
        if (
            previous is not None
            and previous.count <= len(messages)
            and previous.hash
            == _fingerprint(configuration, messages[: previous.count], tools)
        ):
            suffix = messages[previous.count :]
            return previous.input_tokens + (
                len(_json_bytes(list(suffix))) if suffix else 0
            )
        return estimate_request_tokens(messages, tools)

    def record(
        self,
        configuration: str,
        messages: Sequence[object],
        tools: Sequence[object],
        *,
        used: int | None,
    ) -> None:
        self._prefix = (
            _SentPrefix(
                len(messages), _fingerprint(configuration, messages, tools), used
            )
            if used is not None
            else None
        )

    def clear(self) -> None:
        self._prefix = None
