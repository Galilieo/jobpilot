"""提供登录用户自己的长期记忆管理接口。"""

from fastapi import APIRouter, HTTPException

from jobpilot.api.deps import CurrentUser, DbSession
from jobpilot.db.accounts import lock_active_account
from jobpilot.db.memories import MemoryConflictError, MemoryValue, save_memory
from jobpilot.db.memory_schema import MemoryKey
from jobpilot.services.memories import (
    MANUAL_SOURCE,
    MemoryDelete,
    MemoryPut,
    MemoryValueError,
    MemoryView,
    memory_views,
    validate_memory_value,
)

router = APIRouter(prefix="/api/memories", tags=["memories"])


@router.get("")
async def get_memories(user: CurrentUser, db: DbSession) -> list[MemoryView]:
    return await memory_views(db, user.user_id)


async def _save(
    key: MemoryKey,
    body: MemoryDelete,
    user: CurrentUser,
    db: DbSession,
    value: MemoryValue | None,
) -> MemoryView:
    if not await lock_active_account(db, user.user_id):
        raise HTTPException(403, "账号当前无法维护记忆")
    try:
        row = await save_memory(
            user.user_id,
            db,
            key,
            expected_revision=body.expected_revision,
            value=value,
            source=MANUAL_SOURCE,
        )
        view = MemoryView.from_record(row)
        await db.commit()
    except MemoryConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    return view


@router.put("/{key}")
async def put_memory(
    key: MemoryKey, body: MemoryPut, user: CurrentUser, db: DbSession
) -> MemoryView:
    try:
        value = validate_memory_value(key, body.value)
    except MemoryValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    return await _save(key, body, user, db, value)


@router.delete("/{key}")
async def delete_memory(
    key: MemoryKey, body: MemoryDelete, user: CurrentUser, db: DbSession
) -> MemoryView:
    return await _save(key, body, user, db, None)
