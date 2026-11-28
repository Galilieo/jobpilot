"""记录本轮原话支持的记忆变更，与可撤销动作在同一事务提交。"""

from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, StringConstraints, ValidationError
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.db.conversations import (
    add_message,
    agent_input,
    find_tool_action,
    owned_agent_run,
    propose_action,
)
from jobpilot.db.memories import (
    count_record_actions,
    find_memory,
    find_writing_preference,
    list_memories,
)
from jobpilot.db.memory_schema import MEMORY_WRITES_PER_RUN, WritingField
from jobpilot.db.models import (
    ActionStatus,
    AgentAction,
    Message,
    MessageRole,
    RunStatus,
)
from jobpilot.services.agent.context import summarize_tool_call
from jobpilot.services.agent.types import Payload, ToolArgs, ToolContext, ToolError
from jobpilot.services.memories import (
    MANUAL_SOURCE,
    MemoryError,
    MemoryInput,
    MemoryReference,
    active_memory,
    delete_memory,
    require_memory_account,
    restore_memory,
    save_memory,
)
from jobpilot.services.writing_preferences import (
    WritingPreferences,
    options_from_row,
    save_writing_preference,
)

RECORD_TOOLS = frozenset({"remember", "forget", "set_writing_preference"})
type Quote = Annotated[
    str, StringConstraints(strip_whitespace=False, min_length=1, max_length=1000)
]


class RememberArgs(ToolArgs, MemoryInput):
    quote: Quote
    replaces: MemoryReference | None = None


class ForgetArgs(ToolArgs):
    memory_id: MemoryReference
    quote: Quote


class SetWritingPreferenceArgs(ToolArgs):
    field: WritingField
    value: str | None
    quote: Quote


class MemoryUndo(BaseModel):
    memory_id: UUID
    revision: int
    previous: MemoryInput | None
    source: dict[str, str]


class WritingUndo(BaseModel):
    revision: int
    previous: WritingPreferences
    source: dict[str, str]


def _source(message: Message, quote: str) -> dict[str, str]:
    return {
        "kind": "conversation",
        "conversation_id": str(message.conversation_id),
        "message_id": str(message.message_id),
        "quote": quote,
    }


async def _change_entry(
    session: AsyncSession,
    user_id: UUID,
    args: RememberArgs | ForgetArgs,
    source: dict[str, str],
) -> tuple[Payload, Payload | None]:
    rows = await list_memories(user_id, session)
    reference = args.memory_id if isinstance(args, ForgetArgs) else args.replaces
    current = active_memory(rows, reference) if reference else None
    previous = (
        MemoryInput(category=current.category, content=current.content)
        if current
        else None
    )
    previous_source = current.source if current else MANUAL_SOURCE
    if isinstance(args, ForgetArgs):
        assert current is not None
        row = current
        delete_memory(row)
        result = {"status": "forgotten", "content": row.content}
    else:
        entry = MemoryInput(category=args.category, content=args.content)
        row, changed = await save_memory(session, user_id, rows, entry, source, current)
        result = {
            "status": "remembered" if changed else "exists",
            "content": row.content,
        }
        if not changed:
            return {**result, "memory_id": str(row.memory_id)}, None
    undo = MemoryUndo(
        memory_id=row.memory_id,
        revision=row.revision,
        previous=previous,
        source=previous_source,
    )
    return {**result, "memory_id": str(row.memory_id)}, {
        "record_type": "memory",
        **undo.model_dump(mode="json"),
    }


async def _change_preference(
    session: AsyncSession,
    user_id: UUID,
    args: SetWritingPreferenceArgs,
    source: dict[str, str],
) -> tuple[Payload, Payload | None]:
    row = await find_writing_preference(session, user_id)
    previous = options_from_row(row)
    old_source = row.source if row else MANUAL_SOURCE
    try:
        values = WritingPreferences.model_validate(
            {**previous.model_dump(), args.field: args.value}
        )
    except ValidationError:
        raise MemoryError("写作偏好必须使用给定选项或默认值", 422) from None
    result = {"field": args.field, "value": args.value}
    if previous == values:
        return {"status": "exists", **result}, None
    updated = await save_writing_preference(
        session, user_id, row, values, row.revision if row else 0, source
    )
    undo = WritingUndo(revision=updated.revision, previous=previous, source=old_source)
    return {"status": "preference_set", **result}, {
        "record_type": "writing_preference",
        **undo.model_dump(mode="json"),
    }


async def record_memory(
    context: ToolContext,
    args: RememberArgs | ForgetArgs | SetWritingPreferenceArgs,
) -> Payload:
    agent = context.agent
    try:
        async with agent.sessions.begin() as session:
            return await _record(session, context, args)
    except MemoryError as error:
        result = {"status": error.status, "error": str(error)}
        if error.entries is not None:
            result["entries"] = error.entries
        return result


async def _record(
    session: AsyncSession,
    context: ToolContext,
    args: RememberArgs | ForgetArgs | SetWritingPreferenceArgs,
) -> Payload:
    agent, call = context.agent, context.call
    await require_memory_account(session, agent.user_id)
    run = await owned_agent_run(
        agent.user_id, session, agent.conversation_id, agent.run_id, lock=True
    )
    if run is None or run.status != RunStatus.RUNNING:
        raise MemoryError("对话运行已结束", 409)
    prior = await find_tool_action(session, agent.user_id, run.run_id, call["key"])
    if prior is not None:
        return prior.result_json or {"status": 409, "error": "旧版记忆操作已失效"}
    message = await agent_input(agent.user_id, session, run.run_id)
    if (
        message is None
        or context.message_used_as_jd
        or not args.quote.strip()
        or args.quote not in message.content
    ):
        raise MemoryError("记忆依据必须逐字来自本轮用户正文，且正文未被用作 JD", 422)
    if (
        await count_record_actions(session, agent.user_id, run.run_id)
        >= MEMORY_WRITES_PER_RUN
    ):
        raise MemoryError("本轮已达到记忆写入上限", 422)

    source = _source(message, args.quote)
    if isinstance(args, SetWritingPreferenceArgs):
        result, undo = await _change_preference(session, agent.user_id, args, source)
    else:
        result, undo = await _change_entry(session, agent.user_id, args, source)
    if undo is None:
        return result
    action = await propose_action(
        session, run, call["name"], call["key"], args.model_dump(mode="json")
    )
    action.status = ActionStatus.EXECUTED
    action.decided_at = func.now()
    action.undo_json = undo
    action.result_json = {
        **result,
        "quote": args.quote,
        "action_id": str(action.action_id),
    }
    await add_message(
        session,
        run,
        MessageRole.TOOL,
        "tool:" + call["key"],
        "",
        steps=[summarize_tool_call(call, action.result_json)],
    )
    return action.result_json


async def undo_memory(
    session: AsyncSession, action: AgentAction, undo: Payload
) -> Payload:
    try:
        if undo.get("record_type") == "writing_preference":
            saved = WritingUndo.model_validate(undo)
            row = await find_writing_preference(session, action.user_id)
            updated = await save_writing_preference(
                session,
                action.user_id,
                row,
                saved.previous,
                saved.revision,
                saved.source,
            )
            return {"revision": updated.revision}
        if undo.get("record_type") != "memory":
            raise MemoryError("旧版记忆操作已迁移，无法撤销", 409)
        saved = MemoryUndo.model_validate(undo)
        row = await find_memory(action.user_id, session, saved.memory_id)
        if row is None or row.revision != saved.revision:
            raise MemoryError("记忆已在别处更新，无法撤销", 409)
        if saved.previous is None:
            delete_memory(row)
        else:
            await restore_memory(session, row, saved.previous, saved.source)
        return {"memory_id": str(row.memory_id), "revision": row.revision}
    except MemoryError as error:
        raise ToolError(str(error), error.status) from None
