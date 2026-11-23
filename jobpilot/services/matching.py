"""JD 与经历事实库的匹配分析：模型逐条对照，程序校验引用、判定表达缺口并汇总。

只接收已发布（confirmed）的事实；绝不输出笼统的匹配度总分或百分比。
"""

import json
from collections import Counter
from collections.abc import Sequence
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.config import ModelPurpose
from jobpilot.db.models import Fact, FactKind, FactSource, Responsibility
from jobpilot.services.facts import TruncatedOutputError
from jobpilot.services.jobs import ParsedJob, Requirement, RequirementType
from jobpilot.services.llm_gateway import (
    ModelBudget,
    request_completion,
    wrap_untrusted_text,
)
from jobpilot.services.prompts import load_prompt


class ModelMatchVerdict(StrEnum):
    """模型输出允许的三种判断；expression_gap 只能由程序判定。"""

    COVERED = "covered"
    PARTIAL = "partial"
    MISSING = "missing"


class MatchVerdict(StrEnum):
    """最终匹配报告里的四种判断。"""

    COVERED = "covered"
    EXPRESSION_GAP = "expression_gap"
    PARTIAL = "partial"
    MISSING = "missing"


class _ModelOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RawMatchItem(_ModelOutput):
    requirement_id: UUID
    verdict: ModelMatchVerdict
    fact_ids: list[UUID] = Field(default_factory=list, max_length=20)
    reason: str = Field(min_length=1, max_length=500)
    suggestion: str = Field(min_length=1, max_length=500)


class RawMatchOutput(_ModelOutput):
    items: list[RawMatchItem] = Field(max_length=80)
    main_gaps: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(
        default_factory=list, max_length=10
    )


class FactCandidate(BaseModel):
    """参与匹配的一条已发布事实。"""

    fact_id: UUID
    user_id: UUID
    fact_set_id: UUID
    lineage_id: UUID
    experience: str
    kind: FactKind
    responsibility: Responsibility
    text: str
    source: FactSource
    confirmed: bool = True

    @classmethod
    def from_fact(cls, fact: Fact) -> "FactCandidate":
        """已发布 Fact 表只包含确认过的事实，可直接作为匹配候选。"""
        return cls(
            fact_id=fact.fact_id,
            user_id=fact.user_id,
            fact_set_id=fact.fact_set_id,
            lineage_id=fact.lineage_id,
            experience=fact.experience,
            kind=FactKind(fact.kind),
            responsibility=Responsibility(fact.responsibility),
            text=fact.text,
            source=FactSource(fact.source),
            confirmed=True,
        )


class MatchItem(BaseModel):
    requirement_id: UUID
    verdict: MatchVerdict
    fact_ids: list[UUID]
    reason: str
    suggestion: str


class CoverageCounts(BaseModel):
    """按要求类型汇总各判断的条目数量；不计算、不输出笼统分数。"""

    total: int
    covered: int
    expression_gap: int
    partial: int
    missing: int


class BuiltMatchReport(BaseModel):
    items: list[MatchItem]
    main_gaps: list[str]
    hard_summary: CoverageCounts
    preferred_summary: CoverageCounts


class MatchValidationError(ValueError):
    """模型输出的匹配结果违反业务约束（不触发通用 JSON 修复重试）。"""


def parse_match_output(completion: ChatCompletion) -> RawMatchOutput:
    """校验并解析模型输出；截断时直接报错。"""
    choice = completion.choices[0]
    if choice.finish_reason == "length":
        raise TruncatedOutputError("匹配分析的输出超过长度上限，被截断")
    return RawMatchOutput.model_validate_json(choice.message.content or "")


def summarize_coverage(
    requirements: Sequence[Requirement],
    items: Sequence[MatchItem],
    req_type: RequirementType,
) -> CoverageCounts:
    """按要求类型（hard / preferred）分别统计四种判断的数目。"""
    verdict_by_req = {item.requirement_id: item.verdict for item in items}
    filtered = [req for req in requirements if req.type is req_type]
    counts = Counter(
        verdict_by_req[req.requirement_id]
        for req in filtered
        if req.requirement_id in verdict_by_req
    )
    return CoverageCounts(
        total=len(filtered),
        covered=counts[MatchVerdict.COVERED],
        expression_gap=counts[MatchVerdict.EXPRESSION_GAP],
        partial=counts[MatchVerdict.PARTIAL],
        missing=counts[MatchVerdict.MISSING],
    )


def build_match_report(
    *,
    user_id: UUID,
    fact_set_id: UUID,
    requirements: Sequence[Requirement],
    facts: Sequence[FactCandidate],
    raw: RawMatchOutput,
) -> BuiltMatchReport:
    """校验要求覆盖完整性与事实归属/版本，并将纯用户补充的已覆盖项改判为表达缺口。"""
    allowed_facts = {
        fact.fact_id: fact
        for fact in facts
        if (
            fact.user_id == user_id
            and fact.fact_set_id == fact_set_id
            and fact.confirmed
        )
    }
    req_ids = {req.requirement_id for req in requirements}

    raw_by_req: dict[UUID, RawMatchItem] = {}
    for raw_item in raw.items:
        if raw_item.requirement_id in raw_by_req:
            raise MatchValidationError(f"重复的要求 ID：{raw_item.requirement_id}")
        if raw_item.requirement_id not in req_ids:
            raise MatchValidationError(f"未知的要求 ID：{raw_item.requirement_id}")
        raw_by_req[raw_item.requirement_id] = raw_item

    if len(raw_by_req) != len(req_ids):
        missing = [
            str(req.requirement_id)
            for req in requirements
            if req.requirement_id not in raw_by_req
        ]
        raise MatchValidationError(f"遗漏了岗位要求：{', '.join(missing)}")

    items: list[MatchItem] = []
    for req in requirements:
        raw_item = raw_by_req[req.requirement_id]
        fact_ids = list(dict.fromkeys(raw_item.fact_ids))
        for fid in fact_ids:
            if fid not in allowed_facts:
                raise MatchValidationError(
                    f"引用了不可用或不属于当前版本的事实 ID：{fid}"
                )

        if (
            raw_item.verdict in (ModelMatchVerdict.COVERED, ModelMatchVerdict.PARTIAL)
            and not fact_ids
        ):
            raise MatchValidationError(
                f"结论为 {raw_item.verdict.value} 时必须引用至少一条事实"
            )
        if raw_item.verdict is ModelMatchVerdict.MISSING and fact_ids:
            raise MatchValidationError("结论为 missing 时不能引用事实")

        if raw_item.verdict is ModelMatchVerdict.COVERED and all(
            allowed_facts[fid].source is FactSource.USER for fid in fact_ids
        ):
            verdict = MatchVerdict.EXPRESSION_GAP
        else:
            verdict = MatchVerdict(raw_item.verdict.value)

        items.append(
            MatchItem(
                requirement_id=req.requirement_id,
                verdict=verdict,
                fact_ids=fact_ids,
                reason=raw_item.reason,
                suggestion=raw_item.suggestion,
            )
        )

    return BuiltMatchReport(
        items=items,
        main_gaps=list(raw.main_gaps),
        hard_summary=summarize_coverage(requirements, items, RequirementType.HARD),
        preferred_summary=summarize_coverage(
            requirements, items, RequirementType.PREFERRED
        ),
    )


def format_match_messages(
    snapshot: ParsedJob,
    facts: Sequence[FactCandidate],
    *,
    system_prompt: str | None = None,
) -> list[ChatCompletionMessageParam]:
    """把岗位要求与已发布事实包装成不可信数据区块，供网关统一脱敏后发送。

    不向模型传入事实来源（source），避免干扰判断；表达缺口完全由程序根据来源改判。
    """
    prompt_text = (
        system_prompt if system_prompt is not None else load_prompt("match_job").text
    )
    job_payload = {
        "title": snapshot.basic_fields.title,
        "requirements": [
            {
                "requirement_id": str(req.requirement_id),
                "type": req.type.value,
                "text": req.text,
            }
            for req in snapshot.requirements
        ],
        "keywords": snapshot.keywords,
    }
    facts_payload = [
        {
            "fact_id": str(fact.fact_id),
            "experience": fact.experience,
            "kind": fact.kind.value,
            "responsibility": fact.responsibility.value,
            "text": fact.text,
        }
        for fact in facts
    ]
    user_content = (
        wrap_untrusted_text(
            "job_requirements", json.dumps(job_payload, ensure_ascii=False)
        )
        + "\n\n"
        + wrap_untrusted_text("facts", json.dumps(facts_payload, ensure_ascii=False))
    )
    return [
        {"role": "system", "content": prompt_text},
        {"role": "user", "content": user_content},
    ]


async def match_job(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: UUID,
    run_id: UUID,
    *,
    fact_set_id: UUID,
    client: AsyncOpenAI,
    budget: ModelBudget,
    snapshot: ParsedJob,
    facts: Sequence[FactCandidate],
) -> BuiltMatchReport:
    """调用模型逐条对照岗位要求与已发布事实库，校验并返回结构化匹配报告。"""
    prompt = load_prompt("match_job")
    messages = format_match_messages(snapshot, facts, system_prompt=prompt.text)

    def validate(completion: ChatCompletion) -> None:
        raw = parse_match_output(completion)
        build_match_report(
            user_id=user_id,
            fact_set_id=fact_set_id,
            requirements=snapshot.requirements,
            facts=facts,
            raw=raw,
        )

    completion = await request_completion(
        session_factory,
        user_id,
        run_id,
        client=client,
        purpose=ModelPurpose.MATCH,
        prompt_version=prompt.label,
        messages=messages,
        json_output=True,
        budget=budget,
        validate=validate,
    )
    return build_match_report(
        user_id=user_id,
        fact_set_id=fact_set_id,
        requirements=snapshot.requirements,
        facts=facts,
        raw=parse_match_output(completion),
    )
