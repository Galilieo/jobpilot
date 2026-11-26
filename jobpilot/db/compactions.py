"""压缩运行的查询及摘要边界的条件更新。"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.db.models import (
    ActionStatus,
    AgentAction,
    AiRun,
    Conversation,
    Message,
    MessageRole,
    RunStatus,
)
from jobpilot.db.runs import ACTIVE_RUN_STATUSES


async def active_compaction(
    user_id: UUID, session: AsyncSession, conversation_id: UUID
) -> AiRun | None:
    return await session.scalar(
        select(AiRun).where(
            AiRun.user_id == user_id,
            AiRun.conversation_id == conversation_id,
            AiRun.kind == "compact",
            AiRun.status.in_(ACTIVE_RUN_STATUSES),
        )
    )


async def recent_compactions(
    user_id: UUID, session: AsyncSession, conversation_id: UUID, maximum: int
) -> list[AiRun]:
    query = (
        select(AiRun)
        .where(
            AiRun.user_id == user_id,
            AiRun.conversation_id == conversation_id,
            AiRun.kind == "compact",
        )
        .order_by(AiRun.started_at.desc(), AiRun.run_id.desc())
        .limit(maximum)
    )
    return list(await session.scalars(query))


async def lock_compaction(
    user_id: UUID, session: AsyncSession, conversation_id: UUID, run_id: UUID
) -> AiRun | None:
    return await session.scalar(
        select(AiRun)
        .where(
            AiRun.user_id == user_id,
            AiRun.run_id == run_id,
            AiRun.conversation_id == conversation_id,
            AiRun.kind == "compact",
            AiRun.status == RunStatus.RUNNING,
        )
        .with_for_update()
    )


async def advance_summary(
    user_id: UUID,
    session: AsyncSession,
    conversation_id: UUID,
    *,
    previous_revision: int,
    previous_through_seq: int,
    through_seq: int,
    summary_json: dict,
    unchanged_updated_at: datetime,
) -> bool:
    """摘要与覆盖点一起前进；只更新摘要时保留对话列表排序。"""
    result = await session.scalar(
        update(Conversation)
        .where(
            Conversation.user_id == user_id,
            Conversation.conversation_id == conversation_id,
            Conversation.summary_revision == previous_revision,
            Conversation.summary_through_seq == previous_through_seq,
            Conversation.summary_through_seq < through_seq,
        )
        .values(
            summary_json=summary_json,
            summary_through_seq=through_seq,
            summary_revision=previous_revision + 1,
            updated_at=unchanged_updated_at,
        )
        .returning(Conversation.conversation_id)
    )
    return result is not None


async def completed_messages(
    user_id: UUID, session: AsyncSession, conversation_id: UUID, through_seq: int
) -> list[Message]:
    """已结束运行的消息可固定为压缩输入，活动轮次始终留在边界之外。"""
    query = (
        select(Message)
        .join(AiRun, AiRun.run_id == Message.run_id)
        .where(
            Message.user_id == user_id,
            Message.conversation_id == conversation_id,
            Message.seq > through_seq,
            AiRun.kind == "agent",
            AiRun.status.not_in(ACTIVE_RUN_STATUSES),
        )
        .order_by(Message.seq)
    )
    return list(await session.scalars(query))


async def submitted_questions(
    user_id: UUID,
    session: AsyncSession,
    conversation_id: UUID,
    *,
    run_ids: set[UUID],
) -> list[AgentAction]:
    if not run_ids:
        return []
    query = (
        select(AgentAction)
        .where(
            AgentAction.user_id == user_id,
            AgentAction.conversation_id == conversation_id,
            AgentAction.run_id.in_(run_ids),
            AgentAction.tool == "ask_user",
            AgentAction.status.in_((ActionStatus.EXECUTED, ActionStatus.UNDONE)),
        )
        .order_by(AgentAction.decided_at, AgentAction.action_id)
    )
    return list(await session.scalars(query))


async def cited_user_bodies(
    user_id: UUID,
    session: AsyncSession,
    conversation_id: UUID,
    *,
    sequences: set[int],
    through_seq: int,
) -> dict[int, str]:
    rows = await session.execute(
        select(Message.seq, Message.content).where(
            Message.user_id == user_id,
            Message.conversation_id == conversation_id,
            Message.role == MessageRole.USER,
            Message.seq.in_(sequences),
            Message.seq <= through_seq,
        )
    )
    return dict(rows.all())


async def cited_question_cards(
    user_id: UUID,
    session: AsyncSession,
    conversation_id: UUID,
    *,
    action_ids: set[UUID],
    through_seq: int,
) -> list[AgentAction]:
    query = (
        select(AgentAction)
        .join(
            Message,
            (Message.run_id == AgentAction.run_id)
            & (Message.event_key == func.concat("confirm:", AgentAction.action_id)),
        )
        .where(
            AgentAction.user_id == user_id,
            AgentAction.conversation_id == conversation_id,
            AgentAction.action_id.in_(action_ids),
            AgentAction.tool == "ask_user",
            AgentAction.status.in_((ActionStatus.EXECUTED, ActionStatus.UNDONE)),
            Message.seq <= through_seq,
        )
        .order_by(Message.seq)
    )
    return list(await session.scalars(query))
