"""工作台汇总接口：一次返回事实库、不编造记录和岗位进度看板的计数。

只汇总当前用户的数据，不修改任何状态；记住的偏好尚未实现，由前端展示「之后开放」。
"""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.api.deps import CurrentUser, DbSession
from jobpilot.db.applications import latest_application_stages
from jobpilot.db.jobs import list_user_jobs
from jobpilot.db.models import (
    AiRun,
    Fact,
    JobStage,
    TailoredResume,
)
from jobpilot.db.reports import latest_match_reports_by_job
from jobpilot.services import drafts
from jobpilot.services.applications import derive_job_stage
from jobpilot.services.drafts import ItemStatus
from jobpilot.services.jobs import effective_parsed_job
from jobpilot.services.matching import JobMatchSummary, summarize_stored_match
from jobpilot.services.usage_stats import grounding_counts

router = APIRouter(prefix="/api", tags=["overview"])

_APPLIED_STAGES = frozenset(
    {
        JobStage.APPLIED,
        JobStage.WRITTEN_TEST,
        JobStage.INTERVIEWING,
        JobStage.OFFER,
        JobStage.REJECTED,
    }
)


class FactsOverview(BaseModel):
    published_count: int
    version: int | None
    pending_count: int


class GroundingOverview(BaseModel):
    total: int
    passed: int
    fallback: int
    pending: int
    resume_count: int


class BoardJobCard(BaseModel):
    job_id: UUID
    title: str | None
    company: str | None
    salary: str | None
    location: str | None
    onsite_days_per_week: str | None
    education: str | None
    stage: JobStage
    match_summary: JobMatchSummary | None
    updated_at: datetime
    resume_stale: bool


class JobBoard(BaseModel):
    unmatched: list[BoardJobCard]
    matched: list[BoardJobCard]
    tailored: list[BoardJobCard]
    applied: list[BoardJobCard]


class OverviewOut(BaseModel):
    facts: FactsOverview
    grounding: GroundingOverview
    board: JobBoard


async def _facts_overview(
    db: AsyncSession, user_id: UUID
) -> tuple[FactsOverview, UUID | None]:
    draft, latest = await drafts.read_snapshot(db, user_id)
    pending_count = (
        sum(1 for item in drafts.items_of(draft) if item.status is ItemStatus.PENDING)
        + sum(
            1
            for item in drafts.draft_criteria_of(draft)
            if item.status is ItemStatus.PENDING
        )
        + sum(
            1
            for item in drafts.draft_educations_of(draft)
            if item.draft_status is ItemStatus.PENDING
        )
    )
    published_count = 0
    if latest is not None:
        published_count = (
            await db.scalar(
                select(func.count(Fact.fact_id)).where(
                    Fact.user_id == user_id,
                    Fact.fact_set_id == latest.fact_set_id,
                )
            )
            or 0
        )
    overview = FactsOverview(
        published_count=published_count,
        version=latest.version if latest else None,
        pending_count=pending_count,
    )
    return overview, latest.fact_set_id if latest else None


async def _grounding_overview(db: AsyncSession, user_id: UUID) -> GroundingOverview:
    counts = await grounding_counts(db, user_id)
    return GroundingOverview(
        total=counts.total,
        passed=counts.passed,
        fallback=counts.fallback,
        pending=counts.pending,
        resume_count=counts.resume_count,
    )


async def _tailored_fact_sets(
    db: AsyncSession, user_id: UUID
) -> dict[UUID, UUID | None]:
    """每个岗位最近一份定制简历所使用的事实库版本，用于判断简历是否已过期。"""
    rows = (
        await db.execute(
            select(AiRun.user_job_id, AiRun.fact_set_id)
            .join(
                TailoredResume,
                (TailoredResume.run_id == AiRun.run_id)
                & (TailoredResume.user_id == AiRun.user_id),
            )
            .where(TailoredResume.user_id == user_id, AiRun.user_job_id.is_not(None))
            .ext(distinct_on(AiRun.user_job_id))
            .order_by(
                AiRun.user_job_id,
                AiRun.started_at.desc(),
                AiRun.finished_at.desc().nulls_last(),
                TailoredResume.created_at.desc(),
            )
        )
    ).all()
    return {job_id: fact_set_id for job_id, fact_set_id in rows if job_id is not None}


async def _job_board(
    db: AsyncSession, user_id: UUID, latest_fact_set_id: UUID | None
) -> JobBoard:
    jobs = await list_user_jobs(db, user_id)
    latest_reports = await latest_match_reports_by_job(db, user_id)
    tailored_sets = await _tailored_fact_sets(db, user_id)
    stages = await latest_application_stages(user_id, db)

    unmatched: list[BoardJobCard] = []
    matched: list[BoardJobCard] = []
    tailored: list[BoardJobCard] = []
    applied: list[BoardJobCard] = []

    for posting, user_job in jobs:
        stage = derive_job_stage(user_job, stages.get(user_job.user_job_id))
        if stage is JobStage.ARCHIVED:
            continue
        fields = effective_parsed_job(posting, user_job).basic_fields
        job_id = user_job.user_job_id
        has_resume = job_id in tailored_sets
        report = latest_reports.get(job_id)
        card = BoardJobCard(
            job_id=job_id,
            title=fields.title,
            company=fields.company,
            salary=fields.salary,
            location=fields.location,
            onsite_days_per_week=fields.onsite_days_per_week,
            education=fields.education,
            stage=stage,
            match_summary=summarize_stored_match(*report) if report else None,
            updated_at=user_job.updated_at,
            resume_stale=(
                has_resume
                and latest_fact_set_id is not None
                and tailored_sets[job_id] != latest_fact_set_id
            ),
        )
        if stage in _APPLIED_STAGES:
            applied.append(card)
        elif has_resume:
            tailored.append(card)
        elif report is not None:
            matched.append(card)
        else:
            unmatched.append(card)

    return JobBoard(
        unmatched=unmatched, matched=matched, tailored=tailored, applied=applied
    )


@router.get("/overview")
async def get_overview(user: CurrentUser, db: DbSession) -> OverviewOut:
    """一次返回工作台所需的事实库、改写检查和岗位进度看板汇总。"""
    facts, latest_fact_set_id = await _facts_overview(db, user.user_id)
    return OverviewOut(
        facts=facts,
        grounding=await _grounding_overview(db, user.user_id),
        board=await _job_board(db, user.user_id, latest_fact_set_id),
    )
