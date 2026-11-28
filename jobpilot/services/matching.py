"""JD 与经历事实库的匹配分析：模型逐条对照，程序校验引用、判定表达缺口并汇总。

只接收已发布（confirmed）的事实；绝不输出笼统的匹配度总分或百分比。
"""

import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.config import ModelPurpose
from jobpilot.db.models import (
    CriterionField,
    Fact,
    FactKind,
    FactSource,
    Responsibility,
)
from jobpilot.services import drafts
from jobpilot.services.criteria_match import (
    CriterionCandidate,
    EducationCandidate,
    LocationCheck,
    compare_location,
    evaluate_requirement,
)
from jobpilot.services.facts import TruncatedOutputError
from jobpilot.services.jobs import BasicFields, ParsedJob, Requirement, RequirementType
from jobpilot.services.llm_gateway import (
    ModelQuota,
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
    criteria_refs: list[str] = Field(default_factory=list, max_length=20)
    reason: str = Field(min_length=1, max_length=500)
    suggestion: str = Field(min_length=1, max_length=500)


class RawMatchOutput(_ModelOutput):
    items: list[RawMatchItem] = Field(max_length=80)
    # 兼容现有提示词的输出。展示摘要从校验后、带要求 ID 的逐项结果生成。
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
    criteria_refs: list[str] = Field(default_factory=list)
    decision_source: Literal["model", "program"] = "model"
    review_note: str | None = None
    reason: str
    suggestion: str


@dataclass(frozen=True)
class MatchInputs:
    """两个匹配入口共用的按版本加载结果。"""

    facts: tuple[FactCandidate, ...]
    criteria: tuple[CriterionCandidate, ...]
    education: tuple[EducationCandidate, ...]

    @property
    def coverage_criteria(self) -> tuple[CriterionCandidate, ...]:
        return tuple(
            item
            for item in self.criteria
            if item.field
            not in (CriterionField.TARGET_DIRECTION, CriterionField.PREFERRED_CITIES)
        )

    @property
    def is_empty(self) -> bool:
        return not (self.facts or self.coverage_criteria or self.education)

    def location_check(self, location: str | None) -> LocationCheck | None:
        cities = next(
            (
                item.list_value
                for item in self.criteria
                if item.field is CriterionField.PREFERRED_CITIES
            ),
            None,
        )
        return compare_location(location, cities)


async def load_match_inputs(
    db: AsyncSession, user_id: UUID, fact_set_id: UUID
) -> MatchInputs:
    """按 (user_id, fact_set_id) 一次加载匹配所需的已发布事实、求职条件和教育。"""
    fact_rows = await drafts.facts_in(db, user_id, fact_set_id)
    criterion_rows = await drafts.criteria_in(db, user_id, fact_set_id)
    education_rows = await drafts.educations_in(db, user_id, fact_set_id)
    return MatchInputs(
        facts=tuple(FactCandidate.from_fact(row) for row in fact_rows),
        criteria=tuple(CriterionCandidate.from_row(row) for row in criterion_rows),
        education=tuple(EducationCandidate.from_row(row) for row in education_rows),
    )


class CoverageCounts(BaseModel):
    """按要求类型汇总各判断的条目数量；不计算、不输出笼统分数。"""

    total: int
    covered: int
    expression_gap: int
    partial: int
    missing: int


class JobMatchSummary(BaseModel):
    """岗位列表与工作台卡片共用的最新匹配报告摘要；不输出笼统分数。"""

    hard_covered: int
    hard_total: int
    expression_gap: int


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


def summarize_report_counts(
    requirements: Sequence[Requirement], items: Sequence[MatchItem]
) -> tuple[CoverageCounts, CoverageCounts]:
    """报告详情、历史和卡片摘要共用的汇总入口：返回 (硬性要求统计, 加分项统计)。"""
    return (
        summarize_coverage(requirements, items, RequirementType.HARD),
        summarize_coverage(requirements, items, RequirementType.PREFERRED),
    )


def summarize_job_match(
    requirements: Sequence[Requirement], items: Sequence[MatchItem]
) -> JobMatchSummary:
    """由同一份覆盖统计算出岗位卡片摘要：硬性已覆盖（含表达缺口）与总表达缺口数。"""
    hard, preferred = summarize_report_counts(requirements, items)
    return JobMatchSummary(
        hard_covered=hard.covered + hard.expression_gap,
        hard_total=hard.total,
        expression_gap=hard.expression_gap + preferred.expression_gap,
    )


def summarize_stored_match(
    snapshot_json: dict[str, Any] | None, items_json: Sequence[dict[str, Any]]
) -> JobMatchSummary:
    """从数据库快照与结果还原出要求与条目，再走同一汇总函数。"""
    requirements = [
        Requirement.model_validate(req)
        for req in (snapshot_json or {}).get("requirements", [])
    ]
    items = [MatchItem.model_validate(item) for item in items_json]
    return summarize_job_match(requirements, items)


_MAX_MAIN_GAPS = 5


def _summarize_gaps(
    requirements: Sequence[Requirement], items: Sequence[MatchItem]
) -> list[str]:
    """按要求 ID 对齐最终结论，优先列出影响投递的硬性缺口。"""
    by_id = {item.requirement_id: item for item in items}
    return [
        f"{req.text}（{item.reason.rstrip('。')}）"
        for req_type in (RequirementType.HARD, RequirementType.PREFERRED)
        for verdict in (MatchVerdict.MISSING, MatchVerdict.PARTIAL)
        for req in requirements
        if req.type is req_type
        and (item := by_id[req.requirement_id]).verdict is verdict
    ][:_MAX_MAIN_GAPS]


def build_match_report(
    *,
    user_id: UUID,
    fact_set_id: UUID,
    requirements: Sequence[Requirement],
    facts: Sequence[FactCandidate],
    raw: RawMatchOutput,
    criteria: Sequence[CriterionCandidate] = (),
    education: Sequence[EducationCandidate] = (),
    basic_fields: BasicFields | None = None,
) -> BuiltMatchReport:
    """校验引用归属/版本、改判纯用户事实表达缺口，并按程序规则覆写条件与教育要求。"""
    allowed_facts = {
        fact.fact_id: fact
        for fact in facts
        if (
            fact.user_id == user_id
            and fact.fact_set_id == fact_set_id
            and fact.confirmed
        )
    }
    allowed_criteria = {c.ref_id for c in criteria} | {e.ref_id for e in education}
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
        criteria_refs = list(dict.fromkeys(raw_item.criteria_refs))
        for ref_id in criteria_refs:
            if ref_id not in allowed_criteria:
                raise MatchValidationError(
                    f"引用了不可用或不属于当前版本的条件 ID：{ref_id}"
                )

        if (
            raw_item.verdict in (ModelMatchVerdict.COVERED, ModelMatchVerdict.PARTIAL)
            and not fact_ids
            and not criteria_refs
        ):
            raise MatchValidationError(
                f"结论为 {raw_item.verdict.value} 时必须引用至少一条事实或求职条件"
            )
        if raw_item.verdict is ModelMatchVerdict.MISSING and (
            fact_ids or criteria_refs
        ):
            raise MatchValidationError("结论为 missing 时不能引用事实或求职条件")

        # 引用了求职条件或教育的结论永远不改判为表达缺口。
        if (
            raw_item.verdict is ModelMatchVerdict.COVERED
            and not criteria_refs
            and fact_ids
            and all(allowed_facts[fid].source is FactSource.USER for fid in fact_ids)
        ):
            verdict = MatchVerdict.EXPRESSION_GAP
        else:
            verdict = MatchVerdict(raw_item.verdict.value)

        decision, review_note = evaluate_requirement(
            req, criteria, education, basic_fields=basic_fields
        )
        if decision is not None:
            items.append(
                MatchItem(
                    requirement_id=req.requirement_id,
                    verdict=MatchVerdict(decision.verdict),
                    fact_ids=[],
                    criteria_refs=list(decision.criteria_refs),
                    decision_source="program",
                    review_note=None,
                    reason=decision.reason,
                    suggestion=decision.suggestion,
                )
            )
        else:
            items.append(
                MatchItem(
                    requirement_id=req.requirement_id,
                    verdict=verdict,
                    fact_ids=fact_ids,
                    criteria_refs=criteria_refs,
                    decision_source="model",
                    review_note=review_note,
                    reason=raw_item.reason,
                    suggestion=raw_item.suggestion,
                )
            )

    synced_gaps = _summarize_gaps(requirements, items)
    hard_summary, preferred_summary = summarize_report_counts(requirements, items)
    return BuiltMatchReport(
        items=items,
        main_gaps=synced_gaps,
        hard_summary=hard_summary,
        preferred_summary=preferred_summary,
    )


def format_match_messages(
    snapshot: ParsedJob,
    facts: Sequence[FactCandidate],
    *,
    criteria: Sequence[CriterionCandidate] = (),
    education: Sequence[EducationCandidate] = (),
    system_prompt: str | None = None,
) -> list[ChatCompletionMessageParam]:
    """把岗位要求、已发布事实与求职条件/教育包装成不可信数据区块，供网关脱敏后发送。

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
    criteria_payload = [
        {"ref_id": c.ref_id, "kind": "criterion", "label": c.label, "text": c.text}
        for c in criteria
    ] + [
        {"ref_id": e.ref_id, "kind": "education", "label": e.label, "text": e.text}
        for e in education
    ]
    user_content = (
        wrap_untrusted_text(
            "job_requirements", json.dumps(job_payload, ensure_ascii=False)
        )
        + "\n\n"
        + wrap_untrusted_text("facts", json.dumps(facts_payload, ensure_ascii=False))
        + "\n\n"
        + wrap_untrusted_text(
            "criteria", json.dumps(criteria_payload, ensure_ascii=False)
        )
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
    budget: ModelQuota,
    snapshot: ParsedJob,
    facts: Sequence[FactCandidate],
    criteria: Sequence[CriterionCandidate] = (),
    education: Sequence[EducationCandidate] = (),
) -> BuiltMatchReport:
    """调用模型逐条对照岗位要求与已发布事实/条件，校验并覆写后返回匹配报告。"""
    prompt = load_prompt("match_job")
    messages = format_match_messages(
        snapshot,
        facts,
        criteria=criteria,
        education=education,
        system_prompt=prompt.text,
    )

    def validate(completion: ChatCompletion) -> None:
        raw = parse_match_output(completion)
        build_match_report(
            user_id=user_id,
            fact_set_id=fact_set_id,
            requirements=snapshot.requirements,
            facts=facts,
            criteria=criteria,
            education=education,
            basic_fields=snapshot.basic_fields,
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
        criteria=criteria,
        education=education,
        basic_fields=snapshot.basic_fields,
        raw=parse_match_output(completion),
    )
