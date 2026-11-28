"""对话列表、详情与软绑定的 HTTP 接口。"""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StringConstraints

from jobpilot.api.deps import CurrentUser, DbSession, SessionFactory
from jobpilot.db.checkpoints import delete_checkpoint_threads
from jobpilot.db.conversations import (
    conversation_messages,
    conversation_thread,
    find_conversation,
    latest_agent_run,
    latest_confirmation,
    latest_decided_action,
    pending_action,
    set_job_binding,
    undone_record_actions,
)
from jobpilot.db.conversations import (
    list_conversations as list_owned_conversations,
)
from jobpilot.db.jobs import JobLockConflict, lock_job_for_binding
from jobpilot.db.models import Conversation, MessageRole, RunStatus
from jobpilot.db.session import read_snapshot
from jobpilot.services.agent.runtime import (
    ConfirmationStatus,
    card_payload,
    confirmation_status,
)

router = APIRouter(prefix="/api/conversations", tags=["conversations"])
Title = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]


class ConversationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Title = "新对话"
    current_job_id: UUID | None = None


class ConversationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Title | None = None
    current_job_id: UUID | None = None


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    conversation_id: UUID
    title: str
    current_job_id: UUID | None
    created_at: datetime
    updated_at: datetime


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    message_id: UUID
    run_id: UUID
    client_message_id: UUID | None
    role: MessageRole
    content: str
    attachments_json: list[dict[str, Any]]
    steps_json: list[dict[str, Any]]
    created_at: datetime


class AgentRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    run_id: UUID
    status: RunStatus
    stop_reason: str | None


class ConversationDetail(ConversationOut):
    messages: list[MessageOut]
    run: AgentRunOut | None
    pending_action: dict[str, Any] | None
    latest_decided_action: dict[str, Any] | None
    confirmation_status: ConfirmationStatus | None
    undone_record_actions: list[UUID]


async def require_conversation(
    user_id: UUID, db: DbSession, conversation_id: UUID, *, lock: bool = False
) -> Conversation:
    conversation = await find_conversation(user_id, db, conversation_id, lock=lock)
    if conversation is None:
        raise HTTPException(404, "对话不存在")
    return conversation


async def _require_job(db: DbSession, user_id: UUID, job_id: UUID | None) -> None:
    try:
        if job_id is not None and not await lock_job_for_binding(db, user_id, job_id):
            raise HTTPException(404, "岗位不存在")
    except JobLockConflict as error:
        raise HTTPException(409, str(error)) from None


@router.get("")
async def list_conversations(user: CurrentUser, db: DbSession) -> list[ConversationOut]:
    return [
        ConversationOut.model_validate(conversation)
        for conversation in await list_owned_conversations(user.user_id, db)
    ]


@router.post("", status_code=201)
async def create_conversation(
    body: ConversationIn, user: CurrentUser, db: DbSession
) -> ConversationOut:
    await _require_job(db, user.user_id, body.current_job_id)
    conversation = Conversation(user_id=user.user_id, **body.model_dump())
    db.add(conversation)
    await db.commit()
    return ConversationOut.model_validate(conversation)


@router.get("/{conversation_id}")
async def get_conversation(
    conversation_id: UUID, user: CurrentUser, sessions: SessionFactory, request: Request
) -> ConversationDetail:
    async with read_snapshot(sessions) as db:
        conversation = await require_conversation(user.user_id, db, conversation_id)
        messages = await conversation_messages(user.user_id, db, conversation_id)
        run = await latest_agent_run(user.user_id, db, conversation_id)
        card = await latest_confirmation(user.user_id, db, run.run_id) if run else None
        decided = await latest_decided_action(user.user_id, db, conversation_id)
        undone = await undone_record_actions(db, user.user_id, conversation_id)
    stream = request.app.state.agent_runtime.streams.get(run.run_id) if run else None
    phase = (
        confirmation_status(run, card, stream.action_id if stream else None)
        if run
        else None
    )
    return ConversationDetail(
        **ConversationOut.model_validate(conversation).model_dump(),
        messages=[MessageOut.model_validate(message) for message in messages],
        run=AgentRunOut.model_validate(run) if run else None,
        pending_action=card_payload(card)
        if card and phase == ConfirmationStatus.WAITING_DECISION
        else None,
        latest_decided_action=(
            {**card_payload(decided), "result": decided.result_json}
            if decided
            else None
        ),
        confirmation_status=phase,
        undone_record_actions=undone,
    )


@router.put("/{conversation_id}")
async def update_conversation(
    conversation_id: UUID, body: ConversationPatch, user: CurrentUser, db: DbSession
) -> ConversationOut:
    conversation = await require_conversation(
        user.user_id, db, conversation_id, lock=True
    )
    if body.title is not None:
        conversation.title = body.title
    if "current_job_id" in body.model_fields_set:
        await _require_job(db, user.user_id, body.current_job_id)
        set_job_binding(conversation, body.current_job_id)
    await db.commit()
    await db.refresh(conversation)
    return ConversationOut.model_validate(conversation)


@router.delete("/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: UUID, user: CurrentUser, db: DbSession
) -> None:
    conversation = await require_conversation(
        user.user_id, db, conversation_id, lock=True
    )
    run = await latest_agent_run(user.user_id, db, conversation_id, active_only=True)
    if run is not None and not await pending_action(user.user_id, db, run.run_id):
        raise HTTPException(409, "对话正在运行，请稍后删除")
    await delete_checkpoint_threads(
        db, [conversation_thread(user.user_id, conversation_id)]
    )
    await db.delete(conversation)
    await db.commit()
