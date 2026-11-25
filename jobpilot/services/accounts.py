"""协调账号删除事务，以及启动和定时清理。"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.db.accounts import (
    LoginSession,
    delete_due_account,
    due_account_ids,
    mark_deletion_requested,
    revoke_account_sessions,
)
from jobpilot.db.runs import cancel_account_runs

logger = logging.getLogger(__name__)
_PURGE_INTERVAL_SECONDS = timedelta(days=1).total_seconds()


async def request_account_deletion(session: AsyncSession, login: LoginSession) -> None:
    """首次申请一次性记录状态、注销会话、取消运行。"""
    if await mark_deletion_requested(session, login):
        user_id = login.user.user_id
        await revoke_account_sessions(session, user_id)
        await cancel_account_runs(session, user_id)
    await session.commit()


@dataclass(frozen=True)
class PurgeResult:
    deleted: int
    failed: int


async def purge_accounts(factory: async_sessionmaker[AsyncSession]) -> PurgeResult:
    """每个账号单独提交，失败的账号留在库里供下次重试。"""
    async with factory() as session:
        candidates = await due_account_ids(session)

    deleted = failed = 0
    for user_id in candidates:
        try:
            async with factory.begin() as session:
                # 改写图未启用 checkpointer。以后启用时，须先按 thread_id 清理检查点。
                removed = await delete_due_account(session, user_id)
            deleted += int(removed)
        except SQLAlchemyError as error:
            failed += 1
            logger.warning("清理到期账号失败，将在下次重试（%s）", type(error).__name__)
    return PurgeResult(deleted=deleted, failed=failed)


async def purge_with_logging(factory: async_sessionmaker[AsyncSession]) -> None:
    """清理故障保留待删除状态，后续仍能按计划再次尝试。"""
    try:
        result = await purge_accounts(factory)
    except SQLAlchemyError as error:
        logger.error("查询到期账号失败，将在下次重试（%s）", type(error).__name__)
    else:
        logger.info(
            "到期账号清理：删除 %d 个，失败 %d 个", result.deleted, result.failed
        )


async def purge_daily(factory: async_sessionmaker[AsyncSession]) -> None:
    """启动清理完成后，在同一进程按日重跑。"""
    while True:
        await asyncio.sleep(_PURGE_INTERVAL_SECONDS)
        await purge_with_logging(factory)
