"""岗位查询共用同一个归属关联，列表、详情和去重均限定当前用户。"""

from uuid import UUID

from sqlalchemy import delete, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.db.idempotency import retire_job_snapshots
from jobpilot.db.models import AiRun, JobPosting, UserJob


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


async def delete_user_job(
    db: AsyncSession, user_id: UUID, job_id: UUID
) -> list[UUID] | None:
    """删除个人岗位及关联运行与产物；私有 posting 无其他引用时一并清理。

    ai_run.(user_job_id, user_id) -> user_job 是延迟检查的 NO ACTION 外键，
    必须先删 ai_run；删它会级联清理 match_report、tailored_bullet、bullet_fact、
    tailored_resume、resume_version、未退役的 idempotency_key，并解除用量的运行引用。
    """
    user_job = await db.scalar(
        select(UserJob)
        .where(UserJob.user_id == user_id, UserJob.user_job_id == job_id)
        .with_for_update()
    )
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
