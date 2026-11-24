"""岗位查询共用同一个归属关联，列表、详情和去重均限定当前用户。"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.db.models import JobPosting, UserJob


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
    db: AsyncSession, user_id: UUID, job_id: UUID, *, for_update: bool = False
) -> tuple[JobPosting, UserJob] | None:
    query = _owned_jobs(user_id).where(UserJob.user_job_id == job_id)
    if for_update:
        query = query.with_for_update(of=UserJob)
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
