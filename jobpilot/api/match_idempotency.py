"""在匹配 POST 注册幂等依赖，并在报告收尾的同一事务内保存响应。"""

from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, HTTPException, status
from fastapi.routing import APIRoute
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.api.deps import CurrentUser, DbSession, SessionFactory
from jobpilot.api.idempotency import RequiredKey, idempotent_request, save_snapshot
from jobpilot.api.matching import create_match_report, get_match_report, router
from jobpilot.db.idempotency import Attempt, KeyScope, Operation
from jobpilot.db.jobs import get_user_job
from jobpilot.db.models import MatchReport


async def reserve_match(
    job_id: UUID,
    user: CurrentUser,
    db: DbSession,
    factory: SessionFactory,
    key: RequiredKey,
) -> AsyncIterator[None]:
    user_id = user.user_id
    if await get_user_job(db, user_id, job_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="岗位不存在")

    async def snapshot(session: AsyncSession, run_id: UUID, attempt: Attempt) -> None:
        report_id = (
            await session.scalars(
                select(MatchReport.report_id).where(
                    MatchReport.run_id == run_id, MatchReport.user_id == user_id
                )
            )
        ).one()
        report = await get_match_report(report_id, user, session)
        await save_snapshot(
            session, attempt, report_id, report, status.HTTP_201_CREATED
        )

    scope = KeyScope(user_id, Operation.MATCH, key)
    async with idempotent_request(db, factory, scope, str(job_id), finish=snapshot):
        yield


def include_matching_routes(app: FastAPI) -> None:
    writes = APIRouter()
    reads = APIRouter()
    for route in router.routes:
        if isinstance(route, APIRoute) and route.endpoint is create_match_report:
            writes.routes.append(route)
        else:
            reads.routes.append(route)
    app.include_router(writes, dependencies=[Depends(reserve_match, scope="function")])
    app.include_router(reads)
