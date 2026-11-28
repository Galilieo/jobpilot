"""启动对话运行、提交确认决定和撤销，通过 SSE 旁听后台执行。"""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func
from starlette.responses import StreamingResponse

from jobpilot.api.deps import CurrentUser, DbSession, ModelClient, SessionFactory
from jobpilot.api.tailoring import Launcher
from jobpilot.db.accounts import lock_active_account
from jobpilot.db.conversations import (
    add_message,
    conversation_thread,
    find_action,
    find_conversation,
    latest_agent_run,
    latest_confirmation,
    owned_agent_run,
    pending_action,
    run_for_client_message,
    run_messages,
)
from jobpilot.db.models import (
    ActionStatus,
    AgentAction,
    AiRun,
    Conversation,
    Message,
    MessageRole,
    RunStatus,
)
from jobpilot.services.agent.actions import undo_write, verify_action_version
from jobpilot.services.agent.application_actions import confirm_application
from jobpilot.services.agent.context import build_memory_snapshot, render_user_content
from jobpilot.services.agent.memory_actions import RECORD_TOOLS
from jobpilot.services.agent.questions import validate_card_decision
from jobpilot.services.agent.runtime import (
    AgentRuntime,
    EventStream,
    card_payload,
    done_event,
)
from jobpilot.services.agent.step_details import resolve_search_steps
from jobpilot.services.agent.tools import TOOL_DEFINITIONS
from jobpilot.services.agent.types import Decision, DecisionIn, Payload, ToolError
from jobpilot.services.agent.window_messages import AgentWindow
from jobpilot.services.prompts import load_prompt
from jobpilot.services.request_window import InputTokenHistory

router = APIRouter(prefix="/api", tags=["agent"])


class TextAttachment(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: Literal["text", "jd", "ocr"] = "text"
    name: str | None = Field(default=None, max_length=200)
    text: str = Field(min_length=1, max_length=20000)


class MessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    client_message_id: UUID
    content: str = Field(default="", max_length=20000)
    attachments: list[TextAttachment] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def validate_text_length(self) -> "MessageIn":
        if not self.content and not self.attachments:
            raise ValueError("请输入消息或添加附件")
        user_input = render_user_content(
            self.content, [item.text for item in self.attachments]
        )
        window = AgentWindow(InputTokenHistory(), TOOL_DEFINITIONS)
        request = window.request(
            [
                {"role": "system", "content": load_prompt("agent").text},
                {"role": "user", "content": user_input},
            ],
            [],
        )
        if request is None:
            raise ValueError("这条消息和附件太长，请拆开发送")
        return self


def _runtime(request: Request) -> AgentRuntime:
    return request.app.state.agent_runtime


Runtime = Annotated[AgentRuntime, Depends(_runtime)]


def _response(stream: EventStream) -> StreamingResponse:
    return StreamingResponse(
        stream.iterate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _replay(
    run: AiRun,
    *,
    card: AgentAction | None = None,
    action: AgentAction | None = None,
    messages: tuple[Message, ...] = (),
    resolved_steps: dict[UUID, list[Payload]] | None = None,
) -> StreamingResponse:
    stream = EventStream()
    steps_map = resolved_steps or {}
    for message in messages:
        if message.role == MessageRole.ASSISTANT and message.content:
            event = "error" if run.status == RunStatus.FAILED else "text"
            key = "message" if event == "error" else "text"
            stream.emit(event, {key: message.content})
        elif message.role == MessageRole.TOOL:
            for step in steps_map.get(message.message_id, message.steps_json):
                if "tool" in step:
                    stream.emit("tool_end", step)
    if card:
        stream.emit("confirm", card_payload(card))
    if action and action.result_json and "error" in action.result_json:
        stream.emit("error", action.result_json)
    stream.emit("done", done_event(run, action))
    stream.close()
    return _response(stream)


async def _reattach(db: DbSession, runtime: Runtime, run: AiRun) -> StreamingResponse:
    if running := runtime.streams.get(run.run_id):
        await db.commit()
        return _response(running)
    card = await pending_action(run.user_id, db, run.run_id)
    messages = await run_messages(run.user_id, db, run.run_id)
    resolved_steps = await resolve_search_steps(db, run.user_id, messages)
    await db.commit()
    return _replay(
        run, card=card, messages=tuple(messages), resolved_steps=resolved_steps
    )


def _launch(
    runtime: AgentRuntime,
    launcher: Launcher,
    sessions: SessionFactory,
    model: ModelClient,
    run: AiRun,
    *,
    action_id: UUID | None = None,
) -> StreamingResponse:
    return _response(
        runtime.start(sessions, model, run, launcher.launch, action_id=action_id)
    )


async def _lock_conversation(
    user_id: UUID,
    db: DbSession,
    conversation_id: UUID,
    *,
    exclusive_account: bool = False,
) -> Conversation:
    if not await lock_active_account(db, user_id, exclusive=exclusive_account):
        raise HTTPException(403, "账号当前无法执行对话操作")
    conversation = await find_conversation(user_id, db, conversation_id, lock=True)
    if conversation is None:
        raise HTTPException(404, "对话不存在")
    return conversation


@router.post("/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: UUID,
    body: MessageIn,
    user: CurrentUser,
    db: DbSession,
    sessions: SessionFactory,
    model: ModelClient,
    runtime: Runtime,
    launcher: Launcher,
) -> StreamingResponse:
    user_id = user.user_id
    conversation = await _lock_conversation(user_id, db, conversation_id)
    if original := await run_for_client_message(
        user_id, db, conversation_id, body.client_message_id
    ):
        return await _reattach(db, runtime, original)
    if active := await latest_agent_run(user_id, db, conversation_id, active_only=True):
        card = await pending_action(user_id, db, active.run_id)
        raise HTTPException(
            409,
            {
                "message": "这条消息没有保存",
                "run_id": str(active.run_id),
                "pending_action": card_payload(card) if card else None,
            },
        )
    snapshot = await build_memory_snapshot(db, user_id)
    if conversation.memory_snapshot_json != snapshot:
        conversation.memory_snapshot_json = snapshot
    run = AiRun(
        kind="agent",
        user_id=user_id,
        conversation_id=conversation_id,
        thread_id=conversation_thread(user_id, conversation_id),
    )
    db.add(run)
    await db.flush()
    await add_message(
        db,
        run,
        MessageRole.USER,
        "input",
        body.content,
        client_message_id=body.client_message_id,
        attachments=[
            attachment.model_dump(mode="json") for attachment in body.attachments
        ],
    )
    conversation.updated_at = func.now()
    await db.commit()
    return _launch(runtime, launcher, sessions, model, run)


async def _store_decision(
    db: DbSession, run: AiRun, action: AgentAction, body: DecisionIn
) -> None:
    card = await pending_action(action.user_id, db, run.run_id)
    if (
        run.status != RunStatus.WAITING_CONFIRMATION
        or card is None
        or card.action_id != action.action_id
    ):
        raise HTTPException(409, "确认卡已失效")
    try:
        validate_card_decision(body, action)
        if body.decision == Decision.CONFIRM:
            await verify_action_version(db, action)
    except ToolError as error:
        raise HTTPException(error.status, str(error)) from None
    action.decision_json = body.model_dump(mode="json")
    action.status = (
        ActionStatus.CONFIRMED
        if body.decision == Decision.CONFIRM
        else ActionStatus.REJECTED
    )
    action.decided_at = func.now()
    if body.decision != Decision.CONFIRM:
        return
    if action.tool in RECORD_TOOLS and "quote" not in action.args_json:
        action.status = ActionStatus.REJECTED
        action.decision_json = {"decision": Decision.REJECT}
        return
    if action.tool == "record_application":
        try:
            await confirm_application(db, action)
        except ToolError as error:
            raise HTTPException(error.status, str(error)) from None


@router.post("/conversations/{conversation_id}/actions/{action_id}")
async def decide_action(
    conversation_id: UUID,
    action_id: UUID,
    body: DecisionIn,
    user: CurrentUser,
    db: DbSession,
    sessions: SessionFactory,
    model: ModelClient,
    runtime: Runtime,
    launcher: Launcher,
) -> StreamingResponse:
    user_id = user.user_id
    await _lock_conversation(user_id, db, conversation_id)
    action = await find_action(user_id, db, action_id, conversation_id=conversation_id)
    if action is None:
        raise HTTPException(404, "确认卡不存在")
    run = await owned_agent_run(user_id, db, conversation_id, action.run_id, lock=True)
    if run is None:
        raise HTTPException(404, "对话运行不存在")
    if action.status == ActionStatus.PENDING:
        await _store_decision(db, run, action, body)
    latest = await latest_confirmation(user_id, db, action.run_id)
    await db.commit()
    if (
        run.status != RunStatus.WAITING_CONFIRMATION
        or latest is None
        or latest.action_id != action_id
    ):
        card = latest if latest and latest.status == ActionStatus.PENDING else None
        return _replay(run, card=card, action=action)
    return _launch(runtime, launcher, sessions, model, run, action_id=action_id)


@router.post("/actions/{action_id}/undo")
async def undo_action(action_id: UUID, user: CurrentUser, db: DbSession) -> Payload:
    action = await find_action(user.user_id, db, action_id)
    if action is None:
        raise HTTPException(404, "确认卡不存在")
    await _lock_conversation(
        user.user_id,
        db,
        action.conversation_id,
        exclusive_account=action.tool in RECORD_TOOLS,
    )
    action = await find_action(user.user_id, db, action_id, lock=True)
    if action is None:
        raise HTTPException(404, "确认卡不存在")
    try:
        result = await undo_write(db, action)
    except ToolError as error:
        raise HTTPException(error.status, str(error)) from None
    await db.commit()
    return {
        "action_id": str(action_id),
        "status": ActionStatus.UNDONE,
        "result": result,
    }
