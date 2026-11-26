"""在同一运行的模型请求中按完整轮次做临时裁剪。"""

from collections.abc import Sequence
from dataclasses import dataclass

from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolUnionParam

from jobpilot.config import ModelPurpose, settings
from jobpilot.services.agent.types import HistoryBoundary
from jobpilot.services.llm_gateway import (
    request_messages_for_model,
    wrap_untrusted_text,
)
from jobpilot.services.request_window import InputTokenHistory


class CurrentInputOverflow(ValueError):
    """固定内容和本轮输入已经超出模型窗口。"""


@dataclass(frozen=True)
class AgentRequest:
    messages: list[ChatCompletionMessageParam]
    input_tokens: int
    output_tokens: int


def output_reserve() -> int:
    return min(
        settings.agent.max_tokens,
        settings.model_profile(ModelPurpose.AGENT).max_output_tokens,
    )


def _gap(first: int, last: int) -> str:
    return wrap_untrusted_text(
        "omitted_history",
        f"消息 {first}～{last} 本次未包含，可以用 read_conversation_history 查。",
    )


def _omit(
    messages: list[ChatCompletionMessageParam],
    history: list[HistoryBoundary],
    count: int,
) -> list[ChatCompletionMessageParam]:
    if not count:
        return messages
    omitted = history[:count]
    end = 1 + sum(item["count"] for item in omitted)
    system = messages[0]
    notice = _gap(omitted[0]["first_seq"], omitted[-1]["last_seq"])
    return [
        {
            "role": "system",
            "content": f"{system['content']}\n{notice}",
        },
        *messages[end:],
    ]


@dataclass
class AgentWindow:
    tokens: InputTokenHistory
    tools: Sequence[ChatCompletionToolUnionParam]

    @property
    def configuration(self) -> str:
        profile = settings.model_profile(ModelPurpose.AGENT)
        return settings.agent.model_dump_json() + profile.model_dump_json()

    def outbound(
        self, messages: list[ChatCompletionMessageParam]
    ) -> list[ChatCompletionMessageParam]:
        return request_messages_for_model(
            messages, adapter=settings.model_profile(ModelPurpose.AGENT).adapter
        )

    def estimate(self, messages: list[ChatCompletionMessageParam]) -> int:
        return self.tokens.estimate(
            self.configuration, self.outbound(messages), self.tools
        )

    def request(
        self,
        messages: list[ChatCompletionMessageParam],
        history: list[HistoryBoundary],
        *,
        smaller_than: int | None = None,
    ) -> AgentRequest | None:
        window = settings.effective_window(ModelPurpose.AGENT)
        reserve = min(output_reserve(), window - 1)
        if smaller_than is not None:
            target = min(window - reserve, smaller_than - max(1, smaller_than // 5))
        else:
            target = window - reserve
        for count in range(len(history) + 1):
            candidate = _omit(messages, history, count)
            estimated = self.estimate(candidate)
            if estimated <= target:
                return AgentRequest(
                    candidate, estimated, min(reserve, window - estimated)
                )
        return None

    def record(self, request: AgentRequest, used: int | None) -> None:
        self.tokens.record(
            self.configuration,
            self.outbound(request.messages),
            self.tools,
            used=used,
        )
