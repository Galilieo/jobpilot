"""对话与消息的用户隔离查询。"""

from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
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


def conversation_thread(user_id: UUID, conversation_id: UUID) -> str:
    return f"{user_id}:{conversation_id}"


async def find_conversation(
    user_id: UUID,
    session: AsyncSession,
    conversation_id: UUID,
    *,
    lock: bool = False,
) -> Conversation | None:
    query = select(Conversation).where(
        Conversation.user_id == user_id, Conversation.conversation_id == conversation_id
    )
    if lock:
        query = query.with_for_update()
    return await session.scalar(query)


async def list_conversations(
    user_id: UUID, session: AsyncSession
) -> list[Conversation]:
    query = (
        select(Conversation)
        .where(Conversation.user_id == user_id)
        .order_by(Conversation.updated_at.desc(), Conversation.conversation_id)
    )
    return list(await session.scalars(query))


async def conversation_messages(
    user_id: UUID, session: AsyncSession, conversation_id: UUID
) -> list[Message]:
    query = (
        select(Message)
        .where(Message.user_id == user_id, Message.conversation_id == conversation_id)
        .order_by(Message.seq)
    )
    return list(await session.scalars(query))


async def latest_agent_run(
    user_id: UUID,
    session: AsyncSession,
    conversation_id: UUID,
    *,
    active_only: bool = False,
) -> AiRun | None:
    query = select(AiRun).where(
        AiRun.user_id == user_id,
        AiRun.conversation_id == conversation_id,
        AiRun.kind == "agent",
    )
    if active_only:
        query = query.where(
            AiRun.status.in_((RunStatus.RUNNING, RunStatus.WAITING_CONFIRMATION))
        )
    return await session.scalar(
        query.order_by(AiRun.started_at.desc(), AiRun.run_id.desc()).limit(1)
    )


async def owned_agent_run(
    user_id: UUID,
    session: AsyncSession,
    conversation_id: UUID,
    run_id: UUID,
    *,
    lock: bool = False,
) -> AiRun | None:
    query = select(AiRun).where(
        AiRun.user_id == user_id,
        AiRun.conversation_id == conversation_id,
        AiRun.run_id == run_id,
        AiRun.kind == "agent",
    )
    if lock:
        query = query.with_for_update()
    return await session.scalar(query)


async def find_action(
    user_id: UUID,
    session: AsyncSession,
    action_id: UUID,
    *,
    conversation_id: UUID | None = None,
    lock: bool = False,
) -> AgentAction | None:
    query = select(AgentAction).where(
        AgentAction.action_id == action_id, AgentAction.user_id == user_id
    )
    if conversation_id is not None:
        query = query.where(AgentAction.conversation_id == conversation_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return await session.scalar(query)


async def latest_confirmation(
    user_id: UUID, session: AsyncSession, run_id: UUID
) -> AgentAction | None:
    return await session.scalar(
        select(AgentAction)
        .join(
            Message,
            (Message.run_id == AgentAction.run_id)
            & (Message.event_key == func.concat("confirm:", AgentAction.action_id)),
        )
        .where(
            AgentAction.user_id == user_id,
            AgentAction.run_id == run_id,
        )
        .order_by(Message.seq.desc())
        .limit(1)
    )


async def pending_action(
    user_id: UUID,
    session: AsyncSession,
    run_id: UUID,
) -> AgentAction | None:
    action = await latest_confirmation(user_id, session, run_id)
    return action if action and action.status == ActionStatus.PENDING else None


async def add_message(
    session: AsyncSession,
    run: AiRun,
    role: MessageRole,
    key: str,
    content: str,
    *,
    attachments: list[dict[str, Any]] | None = None,
    steps: list[dict[str, Any]] | None = None,
) -> None:
    await session.execute(
        insert(Message)
        .values(
            user_id=run.user_id,
            conversation_id=run.conversation_id,
            run_id=run.run_id,
            role=role,
            event_key=key,
            content=content,
            attachments_json=attachments or [],
            steps_json=steps or [],
        )
        .on_conflict_do_nothing(constraint="uq_message_run_event")
    )


async def propose_action(
    session: AsyncSession,
    run: AiRun,
    tool: str,
    key: str,
    args: dict[str, Any],
) -> AgentAction:
    query = select(AgentAction).where(
        AgentAction.user_id == run.user_id,
        AgentAction.run_id == run.run_id,
        AgentAction.tool_call_id == key,
    )
    action = await session.scalar(query)
    if action is None:
        action = AgentAction(
            user_id=run.user_id,
            conversation_id=run.conversation_id,
            run_id=run.run_id,
            tool=tool,
            tool_call_id=key,
            args_json=args,
        )
        session.add(action)
        await session.flush()
    return action


async def recent_messages(
    user_id: UUID, session: AsyncSession, conversation_id: UUID, turns: int
) -> list[Message]:
    owned = select(Message).where(
        Message.user_id == user_id, Message.conversation_id == conversation_id
    )
    oldest = (
        select(Message.seq)
        .where(
            Message.user_id == user_id,
            Message.conversation_id == conversation_id,
            Message.role == MessageRole.USER,
        )
        .order_by(Message.seq.desc())
        .limit(turns)
    ).subquery()
    query = owned.where(
        Message.seq >= select(func.min(oldest.c.seq)).scalar_subquery()
    ).order_by(Message.seq)
    return list(await session.scalars(query))


async def agent_input(
    user_id: UUID, session: AsyncSession, run_id: UUID
) -> Message | None:
    return await session.scalar(
        select(Message).where(
            Message.user_id == user_id,
            Message.run_id == run_id,
            Message.role == MessageRole.USER,
        )
    )
