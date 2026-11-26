"""对话历史的按序读取和压缩可用的结束轮次。"""

from collections.abc import AsyncIterator
from uuid import UUID

from sqlalchemy import Text, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.db.models import Message


async def history_rows(
    user_id: UUID,
    session: AsyncSession,
    conversation_id: UUID,
    *,
    start_seq: int,
    end_seq: int | None,
    query: str | None,
) -> AsyncIterator[Message]:
    statement = select(Message).where(
        Message.user_id == user_id,
        Message.conversation_id == conversation_id,
        Message.seq >= start_seq,
    )
    if end_seq is not None:
        statement = statement.where(Message.seq <= end_seq)
    if query:
        needle = query.casefold()
        statement = statement.where(
            or_(
                func.strpos(func.lower(Message.content), needle) > 0,
                func.strpos(func.lower(cast(Message.attachments_json, Text)), needle)
                > 0,
                func.strpos(func.lower(cast(Message.steps_json, Text)), needle) > 0,
            )
        )
    rows = await session.stream_scalars(
        statement.order_by(Message.seq).execution_options(yield_per=25)
    )
    try:
        async for row in rows:
            yield row
    finally:
        await rows.close()


async def unsummarized_messages(
    user_id: UUID, session: AsyncSession, conversation_id: UUID, through_seq: int
) -> list[Message]:
    return [
        row
        async for row in history_rows(
            user_id,
            session,
            conversation_id,
            start_seq=through_seq + 1,
            end_seq=None,
            query=None,
        )
    ]
