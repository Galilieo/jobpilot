"""统一管理接口和确认卡的记忆取值校验、栏目顺序及返回格式。"""

from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.db.memories import MemoryValue, list_memories
from jobpilot.db.memory_schema import MEMORY_LIMITS, MemoryKey
from jobpilot.db.models import Memory

MAX_REVISION = 2**63 - 2
ExpectedRevision = Annotated[int, Field(strict=True, ge=0, le=MAX_REVISION)]
MANUAL_SOURCE = {"kind": "manual"}


class MemoryValueError(ValueError):
    """栏目值超出档案支持的范围。"""


class MemoryView(BaseModel):
    key: MemoryKey
    value: MemoryValue | None = None
    revision: int = 0
    deleted: bool = False

    @classmethod
    def from_record(cls, record: Memory) -> "MemoryView":
        return cls(
            key=MemoryKey(record.key),
            value=record.value_json,
            revision=record.revision,
            deleted=record.deleted_at is not None,
        )


class MemoryDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: ExpectedRevision


class MemoryPut(MemoryDelete):
    value: MemoryValue


def _validate_text(value: str, limit: int) -> str:
    if not 1 <= len(value) <= limit or "\x00" in value:
        raise MemoryValueError(f"记忆内容需为 1 至 {limit} 个字符")
    return value


def validate_memory_value(key: MemoryKey, value: MemoryValue) -> MemoryValue:
    limit = MEMORY_LIMITS[key]
    if limit.items:
        if not isinstance(value, list) or len(value) > limit.items:
            raise MemoryValueError(f"这个栏目最多可保存 {limit.items} 项")
        return list(
            dict.fromkeys(
                _validate_text(item.strip(), limit.characters) for item in value
            )
        )
    if not isinstance(value, str):
        raise MemoryValueError("这个栏目只接受一个文本值")
    value = value.strip()
    if limit.choices:
        if value not in limit.choices:
            raise MemoryValueError("这个栏目只接受预设选项")
        return value
    return _validate_text(value, limit.characters)


async def memory_views(session: AsyncSession, user_id: UUID) -> list[MemoryView]:
    records = {MemoryKey(row.key): row for row in await list_memories(user_id, session)}
    return [
        MemoryView.from_record(records[key]) if key in records else MemoryView(key=key)
        for key in MemoryKey
    ]
