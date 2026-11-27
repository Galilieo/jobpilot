"""按账号读取投递、阶段事件以及岗位定制简历的具体版本。"""

from datetime import date
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from jobpilot.db.models import (
    AiRun,
    Application,
    ApplicationStageEvent,
    ResumeVersion,
    TailoredResume,
)

APPLICATION_ORDER = (
    Application.applied_at.desc(),
    Application.created_at.desc(),
    Application.application_id.desc(),
)


async def find_application(
    user_id: UUID,
    session: AsyncSession,
    application_id: UUID,
    *,
    lock: bool = False,
    read_lock: bool = False,
) -> Application | None:
    query = select(Application).where(
        Application.user_id == user_id, Application.application_id == application_id
    )
    if lock or read_lock:
        query = query.with_for_update(read=read_lock)
    return await session.scalar(query)


async def find_by_action(
    user_id: UUID, session: AsyncSession, action_id: UUID
) -> Application | None:
    return await session.scalar(
        select(Application).where(
            Application.user_id == user_id, Application.action_id == action_id
        )
    )


async def list_applications(
    user_id: UUID, session: AsyncSession, job_id: UUID | None
) -> list[tuple[Application, date]]:
    latest_event = (
        select(
            ApplicationStageEvent.application_id,
            ApplicationStageEvent.occurred_at.label("stage_changed_at"),
        )
        .where(ApplicationStageEvent.user_id == user_id)
        .ext(distinct_on(ApplicationStageEvent.application_id))
        .order_by(
            ApplicationStageEvent.application_id,
            ApplicationStageEvent.created_at.desc(),
            ApplicationStageEvent.event_id.desc(),
        )
        .subquery()
    )
    query = (
        select(
            Application,
            func.coalesce(
                latest_event.c.stage_changed_at, Application.applied_at
            ).label("stage_changed_at"),
        )
        .outerjoin(
            latest_event,
            latest_event.c.application_id == Application.application_id,
        )
        .where(Application.user_id == user_id)
        .options(defer(Application.snapshot_html), defer(Application.snapshot_json))
    )
    if job_id is not None:
        query = query.where(Application.user_job_id == job_id)
    rows = (await session.execute(query.order_by(*APPLICATION_ORDER))).all()
    return [(application, stage_changed_at) for application, stage_changed_at in rows]


async def list_events(
    user_id: UUID, session: AsyncSession, application_id: UUID
) -> list[ApplicationStageEvent]:
    return list(
        await session.scalars(
            select(ApplicationStageEvent)
            .where(
                ApplicationStageEvent.user_id == user_id,
                ApplicationStageEvent.application_id == application_id,
            )
            .order_by(ApplicationStageEvent.created_at, ApplicationStageEvent.event_id)
        )
    )


async def latest_application_stages(
    user_id: UUID, session: AsyncSession, job_id: UUID | None = None
) -> dict[UUID, str]:
    """批量取每个岗位按投递日期排序的第一条记录。"""
    query = (
        select(Application.user_job_id, Application.stage)
        .where(Application.user_id == user_id, Application.user_job_id.is_not(None))
        .ext(distinct_on(Application.user_job_id))
        .order_by(Application.user_job_id, *APPLICATION_ORDER)
    )
    if job_id is not None:
        query = query.where(Application.user_job_id == job_id)
    return {key: stage for key, stage in await session.execute(query)}


def _resume_versions(
    user_id: UUID,
) -> Select[tuple[ResumeVersion, TailoredResume, AiRun]]:
    return (
        select(ResumeVersion, TailoredResume, AiRun)
        .join(
            TailoredResume,
            (TailoredResume.resume_id == ResumeVersion.resume_id)
            & (TailoredResume.user_id == ResumeVersion.user_id),
        )
        .join(
            AiRun,
            (AiRun.run_id == TailoredResume.run_id)
            & (AiRun.user_id == TailoredResume.user_id),
        )
        .where(ResumeVersion.user_id == user_id)
    )


async def find_resume_version(
    user_id: UUID, session: AsyncSession, version_id: UUID, *, lock: bool = False
) -> tuple[ResumeVersion, TailoredResume, AiRun] | None:
    query = _resume_versions(user_id).where(
        ResumeVersion.resume_version_id == version_id
    )
    if lock:
        query = query.with_for_update(
            of=ResumeVersion, read=True, key_share=True, nowait=True
        )
    row = (await session.execute(query)).one_or_none()
    return tuple(row) if row else None


async def latest_job_resume(
    user_id: UUID, session: AsyncSession, job_id: UUID
) -> tuple[ResumeVersion, TailoredResume, AiRun] | None:
    row = (
        await session.execute(
            _resume_versions(user_id)
            .where(
                AiRun.user_job_id == job_id,
                ResumeVersion.resume_version_id == TailoredResume.current_version_id,
            )
            .order_by(TailoredResume.created_at.desc(), TailoredResume.resume_id.desc())
            .limit(1)
        )
    ).one_or_none()
    return tuple(row) if row else None


async def current_resume_version(
    user_id: UUID, session: AsyncSession, resume_id: UUID
) -> ResumeVersion | None:
    return await session.scalar(
        _resume_versions(user_id).where(
            TailoredResume.resume_id == resume_id,
            ResumeVersion.resume_version_id == TailoredResume.current_version_id,
        )
    )
