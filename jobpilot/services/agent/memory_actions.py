"""在出卡时绑定记忆原话和版本，在确认时原子提交。"""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.db.conversations import agent_input
from jobpilot.db.memories import (
    MemoryConflictError,
    MemoryValue,
    find_memory,
    save_memory,
)
from jobpilot.db.memory_schema import MEMORY_LIMITS, MemoryKey
from jobpilot.db.models import ActionStatus, AgentAction
from jobpilot.services.agent.arguments import ShortText
from jobpilot.services.agent.types import Payload, ToolArgs, ToolContext, ToolError
from jobpilot.services.memories import (
    ExpectedRevision,
    MemoryValueError,
    validate_memory_value,
)
from jobpilot.services.memory_words import (
    MemoryOperation,
    memory_vocabulary,
    supports_memory_change,
)

MEMORY_TOOLS = frozenset({"remember", "forget"})
MEMORY_CLARIFICATION = "你希望长期记住或忘掉哪项偏好？请明确说出栏目、操作和值。"


class RememberArgs(ToolArgs):
    key: MemoryKey
    op: Literal["set", "add", "remove", "replace"]
    value: MemoryValue
    quote: ShortText


class ForgetArgs(ToolArgs):
    key: MemoryKey
    op: Literal["remove", "clear"]
    value: list[str] | None = None
    quote: ShortText


class PreparedMemory(BaseModel):
    key: MemoryKey
    label: str
    op: MemoryOperation
    value: MemoryValue | None
    quote: str
    expected_revision: ExpectedRevision
    source: dict[str, str]


class MemoryUndo(BaseModel):
    key: MemoryKey
    applied_revision: int
    previous_value: MemoryValue | None
    previous_deleted: bool
    previous_source: dict[str, str]


@dataclass(frozen=True)
class _MemoryProposal:
    key: MemoryKey
    op: MemoryOperation
    value: MemoryValue | None
    quote: str


def _new_value(
    proposal: _MemoryProposal, current_value: MemoryValue | None
) -> MemoryValue | None:
    if proposal.op == MemoryOperation.CLEAR:
        return None
    assert proposal.value is not None
    if proposal.op in {MemoryOperation.SET, MemoryOperation.REPLACE}:
        return proposal.value
    assert isinstance(proposal.value, list)
    current = current_value if isinstance(current_value, list) else []
    if proposal.op == MemoryOperation.REMOVE:
        return [value for value in current if value not in proposal.value]
    return validate_memory_value(
        proposal.key, list(dict.fromkeys([*current, *proposal.value]))
    )


def _proposal(args: RememberArgs | ForgetArgs) -> _MemoryProposal:
    operation = MemoryOperation(args.op)
    is_list = bool(MEMORY_LIMITS[args.key].items)
    if operation == MemoryOperation.CLEAR:
        if args.value is not None:
            raise ToolError("清空栏目无需提供值")
        return _MemoryProposal(args.key, operation, None, args.quote)
    if is_list == (operation == MemoryOperation.SET) or args.value is None:
        raise ToolError("这个栏目不支持该操作")
    return _MemoryProposal(
        args.key,
        operation,
        validate_memory_value(args.key, args.value),
        args.quote,
    )


async def prepare_memory(
    context: ToolContext, args: RememberArgs | ForgetArgs
) -> Payload:
    try:
        proposal = _proposal(args)
        return await _prepare(context, proposal)
    except MemoryValueError as error:
        raise ToolError(str(error)) from None


async def _prepare(context: ToolContext, proposal: _MemoryProposal) -> Payload:
    agent = context.agent
    async with agent.sessions() as session:
        message = await agent_input(agent.user_id, session, agent.run_id)
        if (
            context.message_used_as_jd
            or message is None
            or not supports_memory_change(
                proposal.key,
                proposal.op,
                proposal.value,
                proposal.quote,
                message.content,
                [item["text"] for item in message.attachments_json],
            )
        ):
            return {
                "error": "记忆需要本轮明确的原话",
                "status": 422,
                "clarify": MEMORY_CLARIFICATION,
            }
        row = await find_memory(agent.user_id, session, proposal.key)
    current_value = row.value_json if row else None
    if current_value is None and proposal.op in {
        MemoryOperation.REMOVE,
        MemoryOperation.CLEAR,
    }:
        raise ToolError("这个栏目还没有已保存的记忆", 409)
    return PreparedMemory(
        key=proposal.key,
        label=memory_vocabulary().keys[proposal.key].label,
        op=proposal.op,
        value=_new_value(proposal, current_value),
        quote=proposal.quote,
        expected_revision=row.revision if row else 0,
        source={
            "kind": "conversation",
            "conversation_id": str(agent.conversation_id),
            "message_id": str(message.message_id),
        },
    ).model_dump(mode="json")


async def confirm_memory(session: AsyncSession, action: AgentAction) -> Payload:
    """在决定事务中提交，竞争落败的一方可直接收到 HTTP 409。"""
    args = PreparedMemory.model_validate(action.args_json)
    previous = await find_memory(action.user_id, session, args.key)
    # RETURNING 会刷新同一个 ORM 对象，先复制撤销需要的旧值。
    undo = MemoryUndo(
        key=args.key,
        applied_revision=args.expected_revision + 1,
        previous_value=previous.value_json if previous else None,
        previous_deleted=previous is None or previous.deleted_at is not None,
        previous_source=previous.source if previous else args.source,
    )
    try:
        row = await save_memory(
            action.user_id,
            session,
            args.key,
            expected_revision=args.expected_revision,
            value=args.value,
            source=args.source,
        )
    except MemoryConflictError as error:
        raise ToolError(str(error), 409) from None
    result = {"key": args.key, "op": args.op, "revision": row.revision}
    action.status = ActionStatus.EXECUTED
    action.undo_json = undo.model_dump(mode="json")
    action.result_json = result
    return result


async def undo_memory(
    session: AsyncSession, action: AgentAction, undo: Payload
) -> Payload:
    previous = MemoryUndo.model_validate(undo)
    try:
        row = await save_memory(
            action.user_id,
            session,
            previous.key,
            expected_revision=previous.applied_revision,
            value=None if previous.previous_deleted else previous.previous_value,
            source=previous.previous_source,
        )
    except MemoryConflictError as error:
        raise ToolError(str(error), 409) from None
    return {"key": previous.key, "op": "undo", "revision": row.revision}
