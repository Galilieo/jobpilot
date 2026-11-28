"""提供记忆和写作偏好的按用户查询。写入由服务在账号行锁内完成。"""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.db.models import Memory, WritingPreference


async def list_memories(user_id: UUID, session: AsyncSession) -> list[Memory]:
    return list(
        await session.scalars(
            select(Memory)
            .where(Memory.user_id == user_id, Memory.deleted_at.is_(None))
            .order_by(Memory.created_at, Memory.memory_id)
        )
    )


async def find_memory(
    user_id: UUID, session: AsyncSession, memory_id: UUID
) -> Memory | None:
    return await session.scalar(
        select(Memory).where(Memory.user_id == user_id, Memory.memory_id == memory_id)
    )


async def count_record_actions(
    session: AsyncSession, user_id: UUID, run_id: UUID
) -> int:
    from jobpilot.db.models import ActionStatus, AgentAction

    return (
        await session.scalar(
            select(func.count())
            .select_from(AgentAction)
            .where(
                AgentAction.user_id == user_id,
                AgentAction.run_id == run_id,
                AgentAction.status.in_([ActionStatus.EXECUTED, ActionStatus.UNDONE]),
                AgentAction.undo_json["record_type"].astext.is_not(None),
            )
        )
        or 0
    )


async def find_writing_preference(
    session: AsyncSession, user_id: UUID
) -> WritingPreference | None:
    return await session.get(WritingPreference, user_id)
