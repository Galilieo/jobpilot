"""校验记忆内容并在账号行锁内执行预算、去重与修订检查。"""

from collections import Counter
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.db.accounts import lock_active_account
from jobpilot.db.memories import find_memory, list_memories
from jobpilot.db.memory_schema import (
    MEMORY_BUDGET_CHARS,
    MEMORY_CONTENT_CHARS,
    MEMORY_SHORT_ID_LENGTH,
    MemoryCategory,
)
from jobpilot.db.models import Conversation, Memory

MANUAL_SOURCE = {"kind": "manual"}
type MemoryContent = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=MEMORY_CONTENT_CHARS
    ),
]
type MemoryReference = Annotated[
    str,
    StringConstraints(
        pattern=rf"^([0-9a-f]{{{MEMORY_SHORT_ID_LENGTH}}}|[0-9a-f]{{8}}(-[0-9a-f]{{4}}){{3}}-[0-9a-f]{{12}})$"
    ),
]


class MemoryError(ValueError):
    def __init__(
        self, message: str, status: int, entries: list[dict[str, str]] | None = None
    ):
        super().__init__(message)
        self.status = status
        self.entries = entries


class MemoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: MemoryCategory
    content: MemoryContent


class MemoryRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)


class MemoryEdit(MemoryInput, MemoryRevision):
    pass


class MemoryView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    memory_id: UUID
    category: MemoryCategory
    content: str
    revision: int
    source: dict[str, str]
    conversation_title: str | None = None
    created_at: datetime
    updated_at: datetime


class MemoryCollection(BaseModel):
    entries: list[MemoryView]
    used_chars: int
    budget_chars: int = MEMORY_BUDGET_CHARS


class MemoryWriteResult(BaseModel):
    status: Literal["saved", "exists"]
    entry: MemoryView


async def require_memory_account(session: AsyncSession, user_id: UUID) -> None:
    if not await lock_active_account(session, user_id, exclusive=True):
        raise MemoryError("账号当前无法维护记忆", 403)


def reference_entries(
    rows: list[Memory], *, short: bool = True
) -> list[dict[str, str]]:
    prefixes = [row.memory_id.hex[:MEMORY_SHORT_ID_LENGTH] for row in rows]
    counts = Counter(prefixes)
    return [
        {
            "id": prefix if short and counts[prefix] == 1 else str(row.memory_id),
            "category": row.category,
            "content": row.content,
        }
        for row, prefix in zip(rows, prefixes, strict=True)
    ]


async def memory_collection(session: AsyncSession, user_id: UUID) -> MemoryCollection:
    rows = await list_memories(user_id, session)
    # 来源对话可能已删除；只读取属于当前账号的标题。
    source_ids = {
        row.source["conversation_id"]
        for row in rows
        if row.source.get("conversation_id")
    }
    titles = {}
    if source_ids:
        titles = {
            str(conversation_id): title
            for conversation_id, title in await session.execute(
                select(Conversation.conversation_id, Conversation.title).where(
                    Conversation.user_id == user_id,
                    Conversation.conversation_id.in_(source_ids),
                )
            )
        }
    return MemoryCollection(
        entries=[
            MemoryView.model_validate(row).model_copy(
                update={
                    "conversation_title": titles.get(
                        row.source.get("conversation_id", "")
                    ),
                }
            )
            for row in rows
        ],
        used_chars=sum(len(row.content) for row in rows),
    )


def active_memory(rows: list[Memory], reference: str) -> Memory:
    matches = [row for row in rows if str(row.memory_id).startswith(reference)]
    if len(matches) != 1:
        raise MemoryError("记忆不存在或编号不唯一", 404)
    return matches[0]


async def revision_memory(
    session: AsyncSession,
    user_id: UUID,
    memory_id: UUID,
    expected_revision: int,
) -> Memory:
    row = await find_memory(user_id, session, memory_id)
    if row is None:
        raise MemoryError("记忆不存在", 404)
    if row.revision != expected_revision or row.deleted_at is not None:
        raise MemoryError("记忆已在别处更新，请刷新后重试", 409)
    return row


def check_memory_capacity(
    rows: list[Memory],
    entry: MemoryInput,
    current: Memory | None,
) -> Memory | None:
    existing = next(
        (
            row
            for row in rows
            if row.category == entry.category and row.content.strip() == entry.content
        ),
        None,
    )
    if existing is not None:
        return existing
    used = sum(len(row.content) for row in rows if row is not current)
    if used + len(entry.content) > MEMORY_BUDGET_CHARS:
        raise MemoryError(
            "记忆空间已满，请合并或删减已有条目", 409, reference_entries(rows)
        )
    return None


async def save_memory(
    session: AsyncSession,
    user_id: UUID,
    rows: list[Memory],
    entry: MemoryInput,
    source: dict[str, str],
    current: Memory | None = None,
) -> tuple[Memory, bool]:
    existing = check_memory_capacity(rows, entry, current)
    if existing is not None:
        return existing, False
    if current is None:
        current = Memory(
            user_id=user_id,
            category=entry.category,
            content=entry.content,
            source=source,
        )
        session.add(current)
    else:
        current.category = entry.category
        current.content = entry.content
        current.source = source
        current.revision += 1
        current.updated_at = func.now()
    await session.flush()
    await session.refresh(current)
    return current, True


def delete_memory(row: Memory) -> None:
    row.deleted_at = func.now()
    row.updated_at = func.now()
    row.revision += 1


async def restore_memory(
    session: AsyncSession,
    row: Memory,
    previous: MemoryInput,
    source: dict[str, str],
) -> None:
    rows = await list_memories(row.user_id, session)
    existing = check_memory_capacity(rows, previous, row)
    if existing is not None and existing is not row:
        raise MemoryError("已有相同的记忆，无法撤销", 409)
    row.category = previous.category
    row.content = previous.content
    row.source = source
    row.deleted_at = None
    row.revision += 1
    row.updated_at = func.now()
