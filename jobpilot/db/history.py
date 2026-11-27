"""对话历史的按序读取和压缩可用的结束轮次。"""

from collections.abc import AsyncIterator
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.db.models import ActionStatus, AgentAction, Message


async def legacy_question_card(
    session: AsyncSession, message: Message
) -> AgentAction | None:
    return await session.scalar(
        select(AgentAction).where(
            AgentAction.user_id == message.user_id,
            AgentAction.conversation_id == message.conversation_id,
            AgentAction.run_id == message.run_id,
            AgentAction.tool_call_id == message.event_key.removeprefix("tool:"),
            AgentAction.tool == "ask_user",
            AgentAction.status == ActionStatus.EXECUTED,
        )
    )


async def history_rows(
    user_id: UUID,
    session: AsyncSession,
    conversation_id: UUID,
    *,
    start_seq: int,
    end_seq: int | None,
) -> AsyncIterator[Message]:
    statement = select(Message).where(
        Message.user_id == user_id,
        Message.conversation_id == conversation_id,
        Message.seq >= start_seq,
    )
    if end_seq is not None:
        statement = statement.where(Message.seq <= end_seq)
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
        )
    ]
