"""管理登录用户自己的长期记忆与写作偏好。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import APIRouter, HTTPException, Response

from jobpilot.api.deps import CurrentUser, DbSession
from jobpilot.db.memories import find_writing_preference, list_memories
from jobpilot.services.memories import (
    MANUAL_SOURCE,
    MemoryCollection,
    MemoryEdit,
    MemoryError,
    MemoryInput,
    MemoryRevision,
    MemoryView,
    MemoryWriteResult,
    delete_memory,
    memory_collection,
    require_memory_account,
    revision_memory,
    save_memory,
)
from jobpilot.services.writing_preferences import (
    WritingPreferences,
    WritingPreferenceUpdate,
    WritingPreferenceView,
    save_writing_preference,
    writing_preference_view,
)

router = APIRouter(prefix="/api", tags=["memories"])


@asynccontextmanager
async def _write(user: CurrentUser, db: DbSession) -> AsyncIterator[None]:
    try:
        await require_memory_account(db, user.user_id)
        yield
        await db.commit()
    except MemoryError as error:
        detail = str(error)
        if error.entries is not None:
            raise HTTPException(
                error.status, {"message": detail, "entries": error.entries}
            ) from None
        raise HTTPException(error.status, detail) from None


@router.get("/memories")
async def get_memories(user: CurrentUser, db: DbSession) -> MemoryCollection:
    return await memory_collection(db, user.user_id)


@router.post("/memories")
async def add_memory(
    body: MemoryInput, user: CurrentUser, db: DbSession
) -> MemoryWriteResult:
    async with _write(user, db):
        rows = await list_memories(user.user_id, db)
        row, saved = await save_memory(db, user.user_id, rows, body, MANUAL_SOURCE)
        return MemoryWriteResult(
            status="saved" if saved else "exists", entry=MemoryView.model_validate(row)
        )


@router.put("/memories/{memory_id}")
async def edit_memory(
    memory_id: UUID, body: MemoryEdit, user: CurrentUser, db: DbSession
) -> MemoryWriteResult:
    async with _write(user, db):
        current = await revision_memory(
            db, user.user_id, memory_id, body.expected_revision
        )
        rows = await list_memories(user.user_id, db)
        row, saved = await save_memory(
            db, user.user_id, rows, body, MANUAL_SOURCE, current
        )
        return MemoryWriteResult(
            status="saved" if saved else "exists", entry=MemoryView.model_validate(row)
        )


@router.delete("/memories/{memory_id}", status_code=204)
async def remove_memory(
    memory_id: UUID, body: MemoryRevision, user: CurrentUser, db: DbSession
) -> Response:
    async with _write(user, db):
        row = await revision_memory(db, user.user_id, memory_id, body.expected_revision)
        delete_memory(row)
    return Response(status_code=204)


@router.get("/writing-preference")
async def get_writing_preference(
    user: CurrentUser, db: DbSession
) -> WritingPreferenceView:
    return writing_preference_view(await find_writing_preference(db, user.user_id))


@router.put("/writing-preference")
async def put_writing_preference(
    body: WritingPreferenceUpdate,
    user: CurrentUser,
    db: DbSession,
) -> WritingPreferenceView:
    async with _write(user, db):
        user_id = user.user_id
        row = await find_writing_preference(db, user_id)
        # 支持更新单项，显式的 null 才恢复该项默认。
        changes = body.model_dump(exclude_unset=True, exclude={"expected_revision"})
        values = WritingPreferences.model_validate(
            {**writing_preference_view(row).values.model_dump(), **changes}
        )
        saved = await save_writing_preference(
            db, user_id, row, values, body.expected_revision, MANUAL_SOURCE
        )
        return writing_preference_view(saved)
