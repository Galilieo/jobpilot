"""记录投递、冻结选定简历版本、追加阶段历史并校验修改修订号。"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.db import applications as records
from jobpilot.db.accounts import lock_active_account
from jobpilot.db.application_schema import CHANNEL_LENGTH, ApplicationStage
from jobpilot.db.idempotency import Operation, retire_result_snapshots
from jobpilot.db.jobs import get_user_job
from jobpilot.db.models import (
    Application,
    ApplicationStageEvent,
    JobStage,
    ResumeVersion,
    UserJob,
)
from jobpilot.services.jobs import effective_parsed_job
from jobpilot.services.resume_content import ResumeContent
from jobpilot.services.resume_export import RenderMode, render_html

Channel = Annotated[str, Field(max_length=CHANNEL_LENGTH)]


class ApplicationError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status


def require_not_future(value: date | None, field: str) -> None:
    if value is not None and value > date.today():
        raise ApplicationError(422, f"{field}不能晚于今天")


class ApplicationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: UUID
    resume_version_id: UUID
    applied_at: date | None = None
    channel: Channel | None = None


class RevisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)


class ApplicationChanges(RevisionInput):
    stage: ApplicationStage | None = None
    occurred_at: date | None = None
    channel: Channel | None = None
    applied_at: date | None = None


class JobSnapshot(BaseModel):
    title: str | None
    company: str | None
    location: str | None
    salary: str | None
    job_url: str | None = None


class ApplicationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    application_id: UUID
    user_job_id: UUID | None
    job_snapshot_json: JobSnapshot
    resume_version_id: UUID | None
    resume_id: UUID | None
    resume_version: int | None
    fact_set_id: UUID | None
    backfilled: bool
    applied_at: date
    channel: str | None
    stage: ApplicationStage
    revision: int
    created_at: datetime
    updated_at: datetime


class ApplicationListItem(ApplicationOut):
    stage_changed_at: date


class StageEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    event_id: UUID
    from_stage: ApplicationStage | None
    to_stage: ApplicationStage
    occurred_at: date
    created_at: datetime


class ApplicationDetail(ApplicationOut):
    snapshot_json: ResumeContent | None
    stage_events: list[StageEventOut]


@dataclass(frozen=True)
class ApplicationSource:
    job: JobSnapshot
    version: ResumeVersion
    fact_set_id: UUID
    resume_generated_at: datetime


def derive_job_stage(job: UserJob, latest: str | None) -> JobStage:
    if job.archived_at is not None:
        return JobStage.ARCHIVED
    return JobStage(latest) if latest is not None else JobStage.SAVED


async def application_source(
    user_id: UUID,
    session: AsyncSession,
    job_id: UUID,
    version_id: UUID | None,
    *,
    lock_job: bool = False,
) -> ApplicationSource:
    try:
        # 岗位和简历都可能正在变更，确认仍可在其他事务结束后重试。
        job = await get_user_job(
            session, user_id, job_id, for_update=lock_job, nowait=lock_job
        )
        if job is None:
            raise ApplicationError(404, "岗位不存在")
        selected = (
            await records.find_resume_version(
                user_id, session, version_id, lock=lock_job
            )
            if version_id is not None
            else await records.latest_job_resume(user_id, session, job_id)
        )
    except OperationalError as error:
        if getattr(error.orig, "sqlstate", None) != "55P03":
            raise
        raise ApplicationError(409, "岗位或简历正在变更，请稍后重试") from None
    if selected is None:
        if version_id is not None:
            raise ApplicationError(404, "简历版本不存在")
        raise ApplicationError(422, "这个岗位还没有定制简历，请先生成")
    version, resume, run = selected
    if run.user_job_id != job_id or run.fact_set_id is None:
        raise ApplicationError(422, "请选择这个岗位的定制简历版本")
    fields = effective_parsed_job(*job).basic_fields
    return ApplicationSource(
        JobSnapshot.model_validate(fields, from_attributes=True),
        version,
        run.fact_set_id,
        records.resume_generated_at(run, resume),
    )


async def record_application(
    user_id: UUID,
    session: AsyncSession,
    body: ApplicationInput,
    *,
    action_id: UUID | None = None,
) -> Application:
    """和调用方的幂等结果在同一事务提交；确认时仅采用卡片绑定的版本。"""
    if action_id is not None:
        existing = await records.find_by_action(user_id, session, action_id)
        if existing is not None:
            return existing
    require_not_future(body.applied_at, "投递日期")
    source = await application_source(
        user_id, session, body.job_id, body.resume_version_id, lock_job=True
    )
    row = _freeze(user_id, body, source, action_id)
    session.add(row)
    await session.flush()
    _append_stage(session, row, None, row.applied_at)
    return row


def _freeze(
    user_id: UUID,
    body: ApplicationInput,
    source: ApplicationSource,
    action_id: UUID | None,
) -> Application:
    version = source.version
    content = ResumeContent.from_stored(version.content_json)
    return Application(
        user_id=user_id,
        user_job_id=body.job_id,
        job_snapshot_json=source.job.model_dump(mode="json"),
        resume_version_id=version.resume_version_id,
        resume_id=version.resume_id,
        resume_version=version.version,
        fact_set_id=source.fact_set_id,
        snapshot_json=version.content_json,
        snapshot_html=render_html(
            content,
            mode=RenderMode.EXPORT,
            version=version.version,
            responsive_screen=True,
        ),
        applied_at=body.applied_at or date.today(),
        channel=body.channel,
        stage=ApplicationStage.APPLIED,
        action_id=action_id,
    )


def _append_stage(
    session: AsyncSession, row: Application, previous: str | None, occurred_at: date
) -> None:
    session.add(
        ApplicationStageEvent(
            user_id=row.user_id,
            application_id=row.application_id,
            from_stage=previous,
            to_stage=row.stage,
            occurred_at=occurred_at,
        )
    )


async def require_application(
    user_id: UUID,
    session: AsyncSession,
    application_id: UUID,
    *,
    revision: int | None = None,
) -> Application:
    row = await records.find_application(
        user_id, session, application_id, lock=revision is not None
    )
    if row is None:
        raise ApplicationError(404, "投递记录不存在")
    if revision is not None and row.revision != revision:
        raise ApplicationError(409, "投递记录刚被别处改过，请重新读取")
    return row


async def applications_for_job(
    user_id: UUID, session: AsyncSession, job_id: UUID | None
) -> list[ApplicationListItem]:
    if job_id is not None and await get_user_job(session, user_id, job_id) is None:
        raise ApplicationError(404, "岗位不存在")
    rows = await records.list_applications(user_id, session, job_id)
    return [
        ApplicationListItem(
            **ApplicationOut.model_validate(application).model_dump(),
            stage_changed_at=stage_changed_at,
        )
        for application, stage_changed_at in rows
    ]


async def application_detail(
    user_id: UUID, session: AsyncSession, application_id: UUID
) -> ApplicationDetail:
    # 同一读事务保持阶段与追加历史的一致性。
    row = await records.find_application(
        user_id, session, application_id, read_lock=True
    )
    if row is None:
        raise ApplicationError(404, "投递记录不存在")
    events = await records.list_events(user_id, session, application_id)
    return ApplicationDetail(
        **ApplicationOut.model_validate(row).model_dump(),
        snapshot_json=ResumeContent.from_stored(row.snapshot_json)
        if row.snapshot_json is not None
        else None,
        stage_events=[StageEventOut.model_validate(event) for event in events],
    )


async def update_application(
    user_id: UUID,
    session: AsyncSession,
    application_id: UUID,
    changes: ApplicationChanges,
) -> Application:
    await _require_active_writer(session, user_id)
    row = await require_application(
        user_id, session, application_id, revision=changes.expected_revision
    )
    require_not_future(changes.applied_at, "投递日期")
    require_not_future(changes.occurred_at, "阶段日期")
    values = changes.model_dump(
        exclude_unset=True, include={"stage", "channel", "applied_at"}
    )
    modified = {
        key: value
        for key, value in values.items()
        if (value is not None or key == "channel") and value != getattr(row, key)
    }
    previous = row.stage
    for key, value in modified.items():
        setattr(row, key, value)
    if "applied_at" in modified:
        await records.correct_initial_date(
            user_id, session, application_id, row.applied_at
        )
    if row.stage != previous:
        _append_stage(session, row, previous, changes.occurred_at or date.today())
    if modified:
        row.revision += 1
        await session.flush()
        await session.refresh(row)
    return row


async def delete_application(
    user_id: UUID, session: AsyncSession, application_id: UUID, expected_revision: int
) -> None:
    await _require_active_writer(session, user_id)
    row = await require_application(
        user_id, session, application_id, revision=expected_revision
    )
    await retire_result_snapshots(
        session, user_id, Operation.RECORD_APPLICATION, application_id
    )
    await session.delete(row)


async def _require_active_writer(session: AsyncSession, user_id: UUID) -> None:
    if not await lock_active_account(session, user_id):
        raise ApplicationError(403, "账号当前无法修改投递记录")
