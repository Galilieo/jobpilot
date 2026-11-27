"""按服务器本地日统计同一账号在所有用途上的模型预扣。"""

from datetime import datetime, time, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.db.models import ModelCall


async def lock_daily_quota(session: AsyncSession, user_id: UUID) -> None:
    # 事务内串行化同一账号的查询和登记，模型请求期间不持锁。
    key = func.hashtextextended(f"jobpilot:model-quota:{user_id}", 0)
    await session.execute(select(func.pg_advisory_xact_lock(key)))


async def daily_reserved_tokens(
    session: AsyncSession, user_id: UUID, now: datetime
) -> int:
    today = now.date()
    start = datetime.combine(today, time.min).astimezone()
    end = datetime.combine(today + timedelta(days=1), time.min).astimezone()
    reserved = func.coalesce(
        ModelCall.reserved_tokens,
        func.coalesce(ModelCall.prompt_tokens, 0)
        + func.coalesce(ModelCall.completion_tokens, 0),
    )
    return await session.scalar(
        select(func.coalesce(func.sum(reserved), 0)).where(
            ModelCall.user_id == user_id,
            ModelCall.created_at >= start,
            ModelCall.created_at < end,
        )
    )
