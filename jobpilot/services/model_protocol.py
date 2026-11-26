"""隔离 Chat Completions 服务商的请求字段与超长错误识别。"""

import re
from typing import Any

from openai import APIStatusError, omit
from openai.types import CompletionUsage
from openai.types.chat import ChatCompletion

from jobpilot.config import ModelSettings, ProtocolAdapter

_DEEPSEEK_OVERFLOW = re.compile(
    r"maximum context length|context (?:window|length).{0,80}"
    r"(?:exceed|too (?:long|large))"
    r"|exceed.{0,40}context (?:window|length)",
    re.IGNORECASE,
)


def protocol_parameters(
    adapter: ProtocolAdapter, model: ModelSettings
) -> dict[str, Any]:
    if adapter == ProtocolAdapter.DEEPSEEK:
        return {
            "temperature": omit if model.thinking else model.temperature,
            "extra_body": {
                "thinking": {"type": "enabled" if model.thinking else "disabled"}
            },
        }
    return {"temperature": model.temperature}


def context_overflow(adapter: ProtocolAdapter, error: APIStatusError) -> bool:
    if error.status_code != 400:
        return False
    if error.code == "context_length_exceeded":
        return True
    if adapter != ProtocolAdapter.DEEPSEEK:
        return False
    message = error.body.get("message", "") if isinstance(error.body, dict) else ""
    return isinstance(message, str) and bool(_DEEPSEEK_OVERFLOW.search(message))


def completion_usage(
    adapter: ProtocolAdapter, response: ChatCompletion
) -> CompletionUsage | None:
    # 两个 Chat Completions 协议的总输入和输出字段一致，缓存明细留在上游原结构。
    if adapter in {ProtocolAdapter.DEEPSEEK, ProtocolAdapter.OPENAI}:
        return response.usage
    raise ValueError(f"不支持的协议适配器：{adapter}")
