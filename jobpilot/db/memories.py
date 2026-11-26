"""按用户读取记忆，用原子条件写入处理创建和更新竞争。"""

from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.db.memory_schema import MemoryKey
from jobpilot.db.models import Memory

type MemoryValue = str | list[str]


class MemoryConflictError(ValueError):
    """预期修订号已失效。"""


async def list_memories(user_id: UUID, session: AsyncSession) -> list[Memory]:
    return list(await session.scalars(select(Memory).where(Memory.user_id == user_id)))


async def find_memory(
    user_id: UUID, session: AsyncSession, key: MemoryKey
) -> Memory | None:
    return await session.get(Memory, (user_id, key))


async def save_memory(
    user_id: UUID,
    session: AsyncSession,
    key: MemoryKey,
    *,
    expected_revision: int,
    value: MemoryValue | None,
    source: dict[str, str],
) -> Memory:
    """只在预期版本仍生效时写入，删除也递增版本并清空值。"""
    values = {
        "value_json": value,
        "revision": expected_revision + 1,
        "deleted_at": func.now() if value is None else None,
        "source": source,
        "updated_at": func.now(),
    }
    if expected_revision == 0:
        if value is None:
            raise MemoryConflictError("记忆尚未建立")
        statement = (
            insert(Memory)
            .values(user_id=user_id, key=key, **values)
            .on_conflict_do_nothing(index_elements=[Memory.user_id, Memory.key])
        )
    else:
        statement = (
            update(Memory)
            .where(
                Memory.user_id == user_id,
                Memory.key == key,
                Memory.revision == expected_revision,
            )
            .values(**values)
        )
    result = await session.scalar(
        statement.returning(Memory).execution_options(populate_existing=True)
    )
    if result is None:
        raise MemoryConflictError("记忆已在别处更新，请刷新后重试")
    return result
