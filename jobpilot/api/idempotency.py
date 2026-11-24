"""为指定付费 POST 领取幂等键，并用原状态码和响应体重放成功请求。"""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from hashlib import sha256
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.db.idempotency import (
    Attempt,
    ClaimConflict,
    KeyScope,
    StoredResponse,
    claim,
)

KEY_HEADER = "Idempotency-Key"
_FINISH_CALLBACK = "idempotency_finish_callback"
FinishCallback = Callable[[AsyncSession, UUID, Attempt], Awaitable[None]]


class IdempotentReplay(Exception):
    def __init__(self, response: StoredResponse):
        self.response = response


async def replay_response(_request: Request, error: IdempotentReplay) -> Response:
    return Response(
        content=error.response.body,
        status_code=error.response.status_code,
        media_type="application/json",
    )


async def require_key(
    request: Request,
    key: Annotated[
        str | None, Header(alias=KEY_HEADER, description="付费请求必填")
    ] = None,
) -> str:
    if (
        key is None
        or not key.strip()
        or len(key) > 255
        or "," in key
        or not key.isascii()
        or not key.isprintable()
        or len(request.headers.getlist(KEY_HEADER)) != 1
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="请提供一个有效的 Idempotency-Key 请求头",
        )
    return key.strip()


RequiredKey = Annotated[str, Depends(require_key)]


@asynccontextmanager
async def idempotent_request(
    db: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    scope: KeyScope,
    payload: str,
    *,
    finish: FinishCallback | None = None,
) -> AsyncIterator[Attempt]:
    """认证后的请求先释放读事务，再用独立短事务领取键。"""
    await db.commit()
    async with session_factory.begin() as reservation:
        outcome = await claim(reservation, scope, sha256(payload.encode()).hexdigest())
    if outcome == ClaimConflict.HASH_MISMATCH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="此 Idempotency-Key 已用于其他请求内容",
        )
    if outcome == ClaimConflict.PROCESSING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="此请求仍在处理中"
        )
    if isinstance(outcome, StoredResponse):
        raise IdempotentReplay(outcome)

    bind_run = outcome.bind_run
    event.listen(db.sync_session, "after_flush", bind_run)
    if finish is not None:
        db.info[_FINISH_CALLBACK] = (finish, outcome)
    try:
        yield outcome
    except Exception:
        await db.rollback()
        async with session_factory.begin() as reservation:
            await outcome.fail(reservation)
        raise
    finally:
        event.remove(db.sync_session, "after_flush", bind_run)
        if finish is not None:
            db.info.pop(_FINISH_CALLBACK)


async def save_snapshot(
    db: AsyncSession,
    attempt: Attempt,
    result_id: UUID,
    body: BaseModel,
    status_code: int,
) -> None:
    response = StoredResponse(status_code, body.model_dump_json())
    await attempt.succeed(db, result_id, response)


async def complete_reserved_product(db: AsyncSession, run_id: UUID) -> None:
    callback = db.info.get(_FINISH_CALLBACK)
    if callback is not None:
        finish, attempt = callback
        await finish(db, run_id, attempt)
