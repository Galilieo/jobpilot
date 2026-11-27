"""岗位查询共用同一个归属关联，列表、详情和去重均限定当前用户。"""

from typing import NoReturn
from uuid import UUID

from sqlalchemy import delete, exists, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.db.idempotency import retire_job_snapshots
from jobpilot.db.models import AiRun, Conversation, JobPosting, UserJob


class JobLockConflict(RuntimeError):
    """岗位或相关对话正在变更，可以稍后重试。"""


def _raise_job_conflict(error: OperationalError) -> NoReturn:
    if getattr(error.orig, "sqlstate", None) != "55P03":
        raise error
    raise JobLockConflict("岗位或对话正在变更，请稍后重试") from None


def _owned_jobs(user_id: UUID):
    return (
        select(JobPosting, UserJob)
        .join(
            UserJob,
            (UserJob.posting_id == JobPosting.posting_id)
            & (UserJob.user_id == JobPosting.owner_id),
        )
        .where(UserJob.user_id == user_id)
    )


async def get_user_job(
    db: AsyncSession,
    user_id: UUID,
    job_id: UUID,
    *,
    for_update: bool = False,
    nowait: bool = False,
) -> tuple[JobPosting, UserJob] | None:
    query = _owned_jobs(user_id).where(UserJob.user_job_id == job_id)
    if for_update:
        query = query.with_for_update(of=UserJob, nowait=nowait)
    result = await db.execute(query)
    row = result.one_or_none()
    return (row.JobPosting, row.UserJob) if row else None


async def find_existing_job(
    db: AsyncSession, user_id: UUID, content_hash: str
) -> UUID | None:
    query = _owned_jobs(user_id).where(JobPosting.content_hash == content_hash)
    return await db.scalar(query.with_only_columns(UserJob.user_job_id))


async def list_user_jobs(
    db: AsyncSession, user_id: UUID
) -> list[tuple[JobPosting, UserJob]]:
    query = _owned_jobs(user_id).order_by(UserJob.updated_at.desc())
    return [(posting, job) for posting, job in await db.execute(query)]


async def lock_job_for_binding(db: AsyncSession, user_id: UUID, job_id: UUID) -> bool:
    """绑定持有对话锁时，遇到正在删除的目标岗位立即返回冲突。"""
    try:
        async with db.begin_nested():
            row = await get_user_job(db, user_id, job_id, for_update=True, nowait=True)
    except OperationalError as error:
        _raise_job_conflict(error)
    return row is not None


async def _lock_bound_conversations(
    db: AsyncSession, user_id: UUID, job_id: UUID
) -> None:
    await db.execute(
        select(Conversation.conversation_id)
        .where(Conversation.user_id == user_id, Conversation.current_job_id == job_id)
        .order_by(Conversation.conversation_id)
        .with_for_update(nowait=True)
    )


async def _lock_job_for_deletion(
    db: AsyncSession, user_id: UUID, job_id: UUID
) -> UserJob | None:
    try:
        await _lock_bound_conversations(db, user_id, job_id)
        row = await get_user_job(db, user_id, job_id, for_update=True, nowait=True)
        if row is not None:
            # 第一次查询之后可能有刚完成的绑定，持岗位锁期间用 NOWAIT 补锁。
            await _lock_bound_conversations(db, user_id, job_id)
    except OperationalError as error:
        _raise_job_conflict(error)
    return row[1] if row is not None else None


async def delete_user_job(
    db: AsyncSession, user_id: UUID, job_id: UUID
) -> list[UUID] | None:
    """删除个人岗位及关联运行与产物；私有 posting 无其他引用时一并清理。

    ai_run.(user_job_id, user_id) -> user_job 是延迟检查的 NO ACTION 外键，
    必须先删 ai_run；删它会级联清理 match_report、tailored_bullet、bullet_fact、
    tailored_resume、resume_version、未退役的 idempotency_key，并解除用量的运行引用。
    """
    user_job = await _lock_job_for_deletion(db, user_id, job_id)
    if user_job is None:
        return None
    posting_id = user_job.posting_id
    await retire_job_snapshots(db, user_id, job_id)

    deleted_run_ids = list(
        await db.scalars(
            delete(AiRun)
            .where(AiRun.user_id == user_id, AiRun.user_job_id == job_id)
            .returning(AiRun.run_id)
        )
    )
    await db.execute(
        delete(UserJob).where(UserJob.user_id == user_id, UserJob.user_job_id == job_id)
    )
    still_referenced = await db.scalar(
        select(exists().where(UserJob.posting_id == posting_id))
    )
    if not still_referenced:
        await db.execute(
            delete(JobPosting).where(
                JobPosting.posting_id == posting_id,
                JobPosting.owner_id == user_id,
                JobPosting.visibility == "private",
            )
        )
    return deleted_run_ids
