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
from jobpilot.db.jobs import list_user_jobs
from jobpilot.db.models import (
    AiRun,
    CheckStatus,
    Fact,
    JobStage,
    MatchReport,
    TailoredBullet,
    TailoredResume,
)
from jobpilot.services import drafts
from jobpilot.services.drafts import ItemStatus
from jobpilot.services.jobs import effective_parsed_job

router = APIRouter(prefix="/api", tags=["overview"])

_APPLIED_STAGES = frozenset(
    {JobStage.APPLIED, JobStage.INTERVIEWING, JobStage.OFFER, JobStage.REJECTED}
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
    location: str | None
    stage: JobStage
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
    rows = (
        await db.execute(
            select(TailoredBullet.check_status, func.count())
            .where(TailoredBullet.user_id == user_id)
            .group_by(TailoredBullet.check_status)
        )
    ).all()
    counts = {status: count for status, count in rows}
    passed = counts.get(CheckStatus.PASSED, 0)
    fallback = counts.get(CheckStatus.FALLBACK, 0)
    pending = counts.get(CheckStatus.PENDING, 0)
    resume_count = (
        await db.scalar(
            select(func.count(TailoredResume.resume_id)).where(
                TailoredResume.user_id == user_id
            )
        )
        or 0
    )
    return GroundingOverview(
        total=passed + fallback + pending,
        passed=passed,
        fallback=fallback,
        pending=pending,
        resume_count=resume_count,
    )


async def _matched_job_ids(db: AsyncSession, user_id: UUID) -> set[UUID]:
    rows = await db.scalars(
        select(AiRun.user_job_id)
        .join(
            MatchReport,
            (MatchReport.run_id == AiRun.run_id)
            & (MatchReport.user_id == AiRun.user_id),
        )
        .where(MatchReport.user_id == user_id, AiRun.user_job_id.is_not(None))
        .distinct()
    )
    return {job_id for job_id in rows.all() if job_id is not None}


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
            .order_by(AiRun.user_job_id, AiRun.started_at.desc())
        )
    ).all()
    return {job_id: fact_set_id for job_id, fact_set_id in rows if job_id is not None}


async def _job_board(
    db: AsyncSession, user_id: UUID, latest_fact_set_id: UUID | None
) -> JobBoard:
    jobs = await list_user_jobs(db, user_id)
    matched_ids = await _matched_job_ids(db, user_id)
    tailored_sets = await _tailored_fact_sets(db, user_id)

    unmatched: list[BoardJobCard] = []
    matched: list[BoardJobCard] = []
    tailored: list[BoardJobCard] = []
    applied: list[BoardJobCard] = []

    for posting, user_job in jobs:
        stage = JobStage(user_job.stage)
        if stage is JobStage.ARCHIVED:
            continue
        fields = effective_parsed_job(posting, user_job).basic_fields
        job_id = user_job.user_job_id
        has_resume = job_id in tailored_sets
        card = BoardJobCard(
            job_id=job_id,
            title=fields.title,
            company=fields.company,
            location=fields.location,
            stage=stage,
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
        elif job_id in matched_ids:
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
