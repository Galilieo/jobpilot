"""依据检查第三层：独立的模型调用判断「引用的事实能不能支撑这句改写」。

只给模型两样东西：改写句子、引用事实的原文（含所属经历名）。不给 JD、不给改写意图，
免得检查器被「想贴合岗位」带偏。温度 0、不开思考，见 config.py 的 ground_check。

整句结论决定过不过；模型指出的片段只当修订提示：不是句子里逐字存在的片段直接丢掉，
结论保留。片段级定位本来就难（RAGTruth 报告的片段级检测分数远低于整句级），
不能让它决定结果。

模型调用失败或输出始终不合格时，按未通过处理（fail-closed），条目进入修订或回退；
额度耗尽不在此列，照常抛出，由改写流程停止整次运行。
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from openai import APIError, AsyncOpenAI
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.config import ModelPurpose
from jobpilot.services.grounding import GroundingFact, GroundingIssue, IssueKind
from jobpilot.services.llm_gateway import (
    EmptyModelResponseError,
    ModelBudget,
    request_completion,
    wrap_untrusted_text,
)
from jobpilot.services.prompts import load_prompt


class SemanticVerdict(StrEnum):
    SUPPORTED = "supported"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"


class UnsupportedSpan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=500)
    reason: str = Field(max_length=500)


class SemanticOutput(BaseModel):
    """模型输出。结论和片段要自洽，不自洽时网关会附上错误让模型修一次。"""

    model_config = ConfigDict(extra="forbid")

    verdict: SemanticVerdict
    unsupported_spans: list[UnsupportedSpan] = Field(max_length=10)

    @model_validator(mode="after")
    def spans_match_verdict(self) -> "SemanticOutput":
        if self.verdict is SemanticVerdict.SUPPORTED and self.unsupported_spans:
            raise ValueError("判为 supported 时 unsupported_spans 必须为空")
        if self.verdict is not SemanticVerdict.SUPPORTED and not self.unsupported_spans:
            raise ValueError("判为 partial 或 unsupported 时至少给出一个不支持的片段")
        return self


@dataclass(frozen=True)
class SemanticResult:
    """verdict 为 None 表示检查没能完成。issues 为空才算通过。"""

    verdict: SemanticVerdict | None
    issues: tuple[GroundingIssue, ...]

    @property
    def passed(self) -> bool:
        return not self.issues


def parse_semantic_output(completion: ChatCompletion) -> SemanticOutput:
    content = completion.choices[0].message.content or ""
    return SemanticOutput.model_validate_json(content)


def format_messages(
    sentence: str, facts: Sequence[GroundingFact], *, system_prompt: str
) -> list[ChatCompletionMessageParam]:
    payload = [{"experience": fact.experience, "text": fact.text} for fact in facts]
    user_content = (
        wrap_untrusted_text("sentence", sentence)
        + "\n\n"
        + wrap_untrusted_text("facts", json.dumps(payload, ensure_ascii=False))
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]


def to_result(sentence: str, output: SemanticOutput) -> SemanticResult:
    """把模型输出转成问题清单。不在句子里的片段丢掉，结论不变。"""
    if output.verdict is SemanticVerdict.SUPPORTED:
        return SemanticResult(output.verdict, ())

    label = "部分没有依据" if output.verdict is SemanticVerdict.PARTIAL else "没有依据"
    spans = [span for span in output.unsupported_spans if span.text in sentence]
    if not spans:
        issue = GroundingIssue(
            kind=IssueKind.SEMANTIC,
            message=f"语义检查认为这句{label}，但没能指出具体是哪一段",
        )
        return SemanticResult(output.verdict, (issue,))
    return SemanticResult(
        output.verdict,
        tuple(
            GroundingIssue(
                kind=IssueKind.SEMANTIC,
                message=f"「{span.text}」{label}：{span.reason}",
                fragment=span.text,
            )
            for span in spans
        ),
    )


async def ground_check(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: UUID,
    run_id: UUID,
    *,
    client: AsyncOpenAI,
    budget: ModelBudget,
    sentence: str,
    facts: Sequence[GroundingFact],
) -> SemanticResult:
    """调用模型判断引用的事实能否支撑这句改写。调用前需已提交所属 ai_run。"""
    prompt = load_prompt("ground_check")
    messages = format_messages(sentence, facts, system_prompt=prompt.text)
    try:
        completion = await request_completion(
            session_factory,
            user_id,
            run_id,
            client=client,
            purpose=ModelPurpose.GROUND_CHECK,
            prompt_version=prompt.label,
            messages=messages,
            json_output=True,
            budget=budget,
            validate=parse_semantic_output,
        )
    except (APIError, ValidationError, EmptyModelResponseError) as error:
        # 上游出错，或修复一次后输出仍不合格：按未通过处理。
        issue = GroundingIssue(
            kind=IssueKind.CHECK_UNAVAILABLE,
            message=f"语义检查没有完成（{type(error).__name__}），按未通过处理",
        )
        return SemanticResult(None, (issue,))
    return to_result(sentence, parse_semantic_output(completion))
