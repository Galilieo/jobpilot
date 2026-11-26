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


def set_job_binding(conversation: Conversation, job_id: UUID | None) -> int:
    """在已锁住的对话上更新绑定，返回供撤销核对的修订号。"""
    conversation.current_job_id = job_id
    conversation.job_binding_revision += 1
    return conversation.job_binding_revision


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


async def run_messages(
    user_id: UUID, session: AsyncSession, run_id: UUID
) -> list[Message]:
    return list(
        await session.scalars(
            select(Message)
            .where(Message.user_id == user_id, Message.run_id == run_id)
            .order_by(Message.seq)
        )
    )


async def run_for_client_message(
    user_id: UUID, session: AsyncSession, conversation_id: UUID, client_message_id: UUID
) -> AiRun | None:
    return await session.scalar(
        select(AiRun)
        .join(Message, Message.run_id == AiRun.run_id)
        .where(
            Message.user_id == user_id,
            Message.conversation_id == conversation_id,
            Message.client_message_id == client_message_id,
            Message.role == MessageRole.USER,
        )
        .with_for_update(of=AiRun)
    )


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


async def latest_decided_action(
    user_id: UUID, session: AsyncSession, conversation_id: UUID
) -> AgentAction | None:
    """已决定卡可跨轮回读，包含后来撤销的动作。"""
    return await session.scalar(
        select(AgentAction)
        .where(
            AgentAction.user_id == user_id,
            AgentAction.conversation_id == conversation_id,
            AgentAction.decided_at.is_not(None),
        )
        .order_by(AgentAction.decided_at.desc(), AgentAction.action_id.desc())
        .limit(1)
    )


async def waiting_agent_runs(session: AsyncSession) -> list[AiRun]:
    """启动时查找仍停在持久化确认点的运行。"""
    return list(
        await session.scalars(
            select(AiRun).where(
                AiRun.kind == "agent", AiRun.status == RunStatus.WAITING_CONFIRMATION
            )
        )
    )


async def add_message(
    session: AsyncSession,
    run: AiRun,
    role: MessageRole,
    key: str,
    content: str,
    *,
    client_message_id: UUID | None = None,
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
            client_message_id=client_message_id,
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
