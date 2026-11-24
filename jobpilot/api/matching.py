"""匹配报告接口：基于已发布事实库与当前岗位快照生成报告，查看历史报告列表与详情。

匹配只使用最新已发布的事实版本（fact_set / fact），不读取草稿（fact_draft）。
开始匹配时将岗位快照与事实版本固定在 ai_run，查看旧报告始终沿用当时的快照与事实。
不输出笼统的匹配度分数。
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from openai import APIConnectionError, APIStatusError
from pydantic import BaseModel, ValidationError
from sqlalchemy import Select, select

from jobpilot.api.deps import CurrentUser, DbSession, ModelClient, SessionFactory
from jobpilot.db.jobs import get_user_job
from jobpilot.db.models import (
    AiRun,
    Fact,
    FactKind,
    FactSet,
    FactSource,
    MatchReport,
    Responsibility,
    RunStatus,
)
from jobpilot.db.runs import set_run_status
from jobpilot.services import drafts
from jobpilot.services.facts import TruncatedOutputError
from jobpilot.services.jobs import (
    ParsedJob,
    RequirementType,
    effective_parsed_job,
)
from jobpilot.services.llm_gateway import (
    BudgetExhaustedError,
    EmptyModelResponseError,
    ModelBudget,
)
from jobpilot.services.matching import (
    CoverageCounts,
    FactCandidate,
    MatchItem,
    MatchValidationError,
    MatchVerdict,
    match_job,
    summarize_coverage,
)

router = APIRouter(prefix="/api", tags=["matching"])

_MATCH_BUDGET = {"remaining_calls": 3, "remaining_tokens": 40_000}
_MODEL_ERRORS = (
    EmptyModelResponseError,
    APIConnectionError,
    APIStatusError,
    BudgetExhaustedError,
    TruncatedOutputError,
    ValidationError,
    MatchValidationError,
)


class CitedFactOut(BaseModel):
    """匹配报告里引用的一条已发布事实。"""

    fact_id: UUID
    lineage_id: UUID
    experience: str
    kind: FactKind
    responsibility: Responsibility
    text: str
    source: FactSource

    @classmethod
    def of(cls, fact: Fact) -> "CitedFactOut":
        return cls.model_validate(fact, from_attributes=True)

    @classmethod
    def from_candidate(cls, candidate: FactCandidate) -> "CitedFactOut":
        return cls(
            fact_id=candidate.fact_id,
            lineage_id=candidate.lineage_id,
            experience=candidate.experience,
            kind=candidate.kind,
            responsibility=candidate.responsibility,
            text=candidate.text,
            source=candidate.source,
        )


class MatchItemOut(BaseModel):
    requirement_id: UUID
    requirement_type: RequirementType
    requirement_text: str
    verdict: MatchVerdict
    facts: list[CitedFactOut]
    reason: str
    suggestion: str


class MatchReportOut(BaseModel):
    report_id: UUID
    run_id: UUID
    job_id: UUID
    fact_set_id: UUID
    fact_set_version: int
    hard_summary: CoverageCounts
    preferred_summary: CoverageCounts
    main_gaps: list[str]
    items: list[MatchItemOut]
    created_at: datetime


class MatchReportSummaryOut(BaseModel):
    report_id: UUID
    run_id: UUID
    job_id: UUID
    fact_set_id: UUID
    fact_set_version: int
    hard_summary: CoverageCounts
    preferred_summary: CoverageCounts
    main_gaps: list[str]
    created_at: datetime


def _hydrate_report_out(
    *,
    report_id: UUID,
    run_id: UUID,
    job_id: UUID,
    fact_set_id: UUID,
    fact_set_version: int,
    created_at: datetime,
    snapshot: ParsedJob,
    items: Sequence[MatchItem],
    main_gaps: Sequence[str],
    cited_by_id: Mapping[UUID, CitedFactOut],
) -> MatchReportOut:
    """用运行当时固定的岗位快照与事实版本填充报告详情。"""
    req_by_id = {req.requirement_id: req for req in snapshot.requirements}
    hydrated_items = [
        MatchItemOut(
            requirement_id=item.requirement_id,
            requirement_type=req_by_id[item.requirement_id].type,
            requirement_text=req_by_id[item.requirement_id].text,
            verdict=item.verdict,
            facts=[cited_by_id[fid] for fid in item.fact_ids],
            reason=item.reason,
            suggestion=item.suggestion,
        )
        for item in items
    ]
    return MatchReportOut(
        report_id=report_id,
        run_id=run_id,
        job_id=job_id,
        fact_set_id=fact_set_id,
        fact_set_version=fact_set_version,
        hard_summary=summarize_coverage(
            snapshot.requirements, items, RequirementType.HARD
        ),
        preferred_summary=summarize_coverage(
            snapshot.requirements, items, RequirementType.PREFERRED
        ),
        main_gaps=list(main_gaps),
        items=hydrated_items,
        created_at=created_at,
    )


@router.post("/jobs/{job_id}/match", status_code=status.HTTP_201_CREATED)
async def create_match_report(
    job_id: UUID,
    user: CurrentUser,
    db: DbSession,
    session_factory: SessionFactory,
    client: ModelClient,
) -> MatchReportOut:
    """针对当前岗位与最新已发布事实库生成一份匹配报告。"""
    # 先校验岗位归属（404），再检查前置条件（422），避免跨用户请求暴露资源存在。
    job_row = await get_user_job(db, user.user_id, job_id)
    if job_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="岗位不存在")
    posting, user_job = job_row

    snapshot = effective_parsed_job(posting, user_job)
    if not snapshot.requirements:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="当前岗位没有可匹配的要求",
        )

    fact_set = await drafts.latest_fact_set(db, user.user_id)
    facts = await drafts.facts_of(db, fact_set)
    if fact_set is None or not facts:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="尚未发布可用的经历事实，请先确认并发布事实库",
        )

    user_id = user.user_id
    user_job_id = user_job.user_job_id
    fact_set_id = fact_set.fact_set_id
    fact_set_version = fact_set.version
    candidates = [FactCandidate.from_fact(fact) for fact in facts]
    cited_by_id = {c.fact_id: CitedFactOut.from_candidate(c) for c in candidates}

    run = AiRun(
        user_id=user_id,
        kind="match",
        user_job_id=user_job_id,
        fact_set_id=fact_set_id,
        job_snapshot_json=snapshot.model_dump(mode="json"),
    )
    db.add(run)
    await db.commit()
    run_id = run.run_id

    try:
        built = await match_job(
            session_factory,
            user_id,
            run_id,
            fact_set_id=fact_set_id,
            client=client,
            budget=ModelBudget(**_MATCH_BUDGET),
            snapshot=snapshot,
            facts=candidates,
        )
    except _MODEL_ERRORS:
        await set_run_status(db, user_id, run_id, RunStatus.FAILED)
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="匹配分析失败，请稍后重试",
        ) from None

    report = MatchReport(
        user_id=user_id,
        run_id=run_id,
        items_json=[item.model_dump(mode="json") for item in built.items],
        main_gaps=built.main_gaps,
    )
    db.add(report)
    await set_run_status(db, user_id, run_id, RunStatus.SUCCEEDED)
    await db.commit()
    await db.refresh(report)

    return _hydrate_report_out(
        report_id=report.report_id,
        run_id=run_id,
        job_id=user_job_id,
        fact_set_id=fact_set_id,
        fact_set_version=fact_set_version,
        created_at=report.created_at,
        snapshot=snapshot,
        items=built.items,
        main_gaps=built.main_gaps,
        cited_by_id=cited_by_id,
    )


def _report_query() -> Select[tuple[MatchReport, AiRun, FactSet]]:
    """按组合外键连接报告、运行与事实版本，只允许同用户链路。"""
    return (
        select(MatchReport, AiRun, FactSet)
        .join(
            AiRun,
            (AiRun.run_id == MatchReport.run_id)
            & (AiRun.user_id == MatchReport.user_id),
        )
        .join(
            FactSet,
            (FactSet.fact_set_id == AiRun.fact_set_id)
            & (FactSet.user_id == AiRun.user_id),
        )
    )


@router.get("/jobs/{job_id}/matches")
async def list_job_matches(
    job_id: UUID, user: CurrentUser, db: DbSession
) -> list[MatchReportSummaryOut]:
    """列出该岗位的历史匹配报告摘要，按生成时间倒序。"""
    if await get_user_job(db, user.user_id, job_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="岗位不存在")

    rows = await db.execute(
        _report_query()
        .where(MatchReport.user_id == user.user_id, AiRun.user_job_id == job_id)
        .order_by(MatchReport.created_at.desc(), MatchReport.report_id.desc())
    )
    summaries: list[MatchReportSummaryOut] = []
    for report, run, fact_set in rows:
        snapshot = ParsedJob.model_validate(run.job_snapshot_json)
        items = [MatchItem.model_validate(item) for item in report.items_json]
        summaries.append(
            MatchReportSummaryOut(
                report_id=report.report_id,
                run_id=run.run_id,
                job_id=job_id,
                fact_set_id=fact_set.fact_set_id,
                fact_set_version=fact_set.version,
                hard_summary=summarize_coverage(
                    snapshot.requirements, items, RequirementType.HARD
                ),
                preferred_summary=summarize_coverage(
                    snapshot.requirements, items, RequirementType.PREFERRED
                ),
                main_gaps=list(report.main_gaps),
                created_at=report.created_at,
            )
        )
    return summaries


@router.get("/match-reports/{report_id}")
async def get_match_report(
    report_id: UUID, user: CurrentUser, db: DbSession
) -> MatchReportOut:
    """按运行固定的岗位快照与事实版本回读一份匹配报告。"""
    row = (
        await db.execute(
            _report_query().where(
                MatchReport.report_id == report_id,
                MatchReport.user_id == user.user_id,
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="匹配报告不存在"
        )

    report, run, fact_set = row.MatchReport, row.AiRun, row.FactSet
    assert run.user_job_id is not None and run.job_snapshot_json is not None
    snapshot = ParsedJob.model_validate(run.job_snapshot_json)
    facts = await drafts.facts_of(db, fact_set)
    cited_by_id = {fact.fact_id: CitedFactOut.of(fact) for fact in facts}
    items = [MatchItem.model_validate(item) for item in report.items_json]

    return _hydrate_report_out(
        report_id=report.report_id,
        run_id=run.run_id,
        job_id=run.user_job_id,
        fact_set_id=fact_set.fact_set_id,
        fact_set_version=fact_set.version,
        created_at=report.created_at,
        snapshot=snapshot,
        items=items,
        main_gaps=report.main_gaps,
        cited_by_id=cited_by_id,
    )
