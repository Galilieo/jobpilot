"""从业务消息重建最近对话，历史工具只携带简短结果。"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import groupby
from json import dumps
from typing import Any
from uuid import UUID

from openai.types.chat import ChatCompletionSystemMessageParam
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.config import settings
from jobpilot.db.conversations import find_conversation
from jobpilot.db.history import unsummarized_messages
from jobpilot.db.memories import list_memories
from jobpilot.db.memory_schema import MEMORY_AUTO_MAX_BODY_CHARS
from jobpilot.db.models import Conversation, Message, MessageRole
from jobpilot.db.session import read_snapshot
from jobpilot.services.agent.read_tools import current_job_summary
from jobpilot.services.agent.step_details import (
    DETAIL_KEY,
    live_step_detail,
    model_view,
    step_detail,
)
from jobpilot.services.agent.types import (
    AgentDeps,
    AgentState,
    HistoryBoundary,
    Payload,
    ToolCall,
)
from jobpilot.services.agent.work_summary import StoredWorkSummary, render_summary
from jobpilot.services.llm_gateway import (
    dump_for_model,
    redact_pii,
    redact_pii_data,
    wrap_untrusted_text,
)
from jobpilot.services.memories import reference_entries
from jobpilot.services.prompts import load_prompt
from jobpilot.services.writing_preferences import writing_preferences_snapshot


def _result_preview(value: object, limit: int) -> object:
    if len(serialize(value).encode()) <= limit:
        return value
    if isinstance(value, str):
        # 脱敏后再裁剪，避免把邮箱截成无法识别的半段。
        return redact_pii(value).encode()[: limit // 2].decode(errors="ignore")
    if isinstance(value, list):
        items: list[object] = []
        for item in value:
            if len(serialize([*items, item]).encode()) > limit:
                break
            items.append(item)
        return items
    if isinstance(value, dict):
        return {
            key: item
            for key, item in value.items()
            if len(serialize(item).encode()) <= limit // max(1, len(value))
        }
    return None


def _strip_internal_keys(value: Any) -> Any:
    """过滤仅用于内部步骤持久化或引用的 `_` 前缀字段。"""
    if isinstance(value, dict):
        return {
            key: _strip_internal_keys(item)
            for key, item in value.items()
            if not (isinstance(key, str) and key.startswith("_"))
        }
    if isinstance(value, list):
        return [_strip_internal_keys(item) for item in value]
    return value


def serialize(value: object) -> str:
    return dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def serialize_tool_result(result: Payload, *, limit: int | None = None) -> str:
    """先过滤内部字段并做脱敏再序列化；超限时在字段边界裁剪。"""
    redacted = redact_pii_data(_strip_internal_keys(result))
    content = serialize(redacted)
    limit = settings.agent_limits.tool_result_bytes if limit is None else limit
    if len(content.encode()) <= limit:
        return content
    preview: Payload = {"truncated": True}
    field_limit = limit // max(1, len(redacted))
    for key, value in redacted.items():
        candidate = {**preview, key: _result_preview(value, field_limit)}
        if len(serialize(candidate).encode()) <= limit:
            preview = candidate
    return serialize(preview)


def summarize_tool(name: str, result: Payload, *, live: bool = False) -> Payload:
    keys = (
        "status",
        "job_id",
        "report_id",
        "run_id",
        "version_id",
        "fact_set_id",
        "stop_reason",
        "total",
        "error",
        "conversation_id",
        "memory_id",
        "decision",
    )
    summary = {key: result[key] for key in keys if key in result}
    if name == "ask_user" and "answers" in result:
        summary["answers"] = result["answers"]
    detail = live_step_detail(name, result) if live else step_detail(name, result)
    if detail is not None:
        summary[DETAIL_KEY] = detail
    return {"tool": name, **summary}


def summarize_tool_call(
    call: ToolCall, result: Payload, *, live: bool = False
) -> Payload:
    return {
        **summarize_tool(call["name"], result, live=live),
        "tool_call_id": call["call_id"],
    }


def render_user_content(content: str, attachments: Sequence[str]) -> str:
    return "\n".join(
        [
            content,
            *(
                wrap_untrusted_text(f"attachment_{index}", text)
                for index, text in enumerate(attachments, 1)
            ),
        ]
    )


def _history_content(message: Message, *, current: bool) -> str:
    if message.role == MessageRole.USER and current:
        return render_user_content(
            message.content, [item["text"] for item in message.attachments_json]
        )
    return message.content


@dataclass(frozen=True)
class SystemContext:
    memories: Payload
    tasks: Payload | None

    def render(self, summary: Payload | None) -> ChatCompletionSystemMessageParam:
        blocks = [load_prompt("agent").text]
        if self.memories:
            blocks.append(
                "长期记忆\n"
                + wrap_untrusted_text(
                    "long_term_memory",
                    dump_for_model(self.memories, separators=(",", ":")),
                )
            )
        if summary:
            work = StoredWorkSummary.model_validate(summary)
            blocks.append(
                "工作摘要\n" + wrap_untrusted_text("work_summary", render_summary(work))
            )
        if self.tasks is not None:
            blocks.append(
                "任务状态\n"
                + wrap_untrusted_text(
                    "task_state",
                    dump_for_model(self.tasks, separators=(",", ":")),
                )
            )
        return {"role": "system", "content": "\n".join(blocks)}


async def build_memory_snapshot(session: AsyncSession, user_id: UUID) -> Payload:
    entries = reference_entries(await list_memories(user_id, session))
    groups: dict[str, list[Payload]] = {}
    for entry in entries:
        groups.setdefault(entry["category"], []).append(
            {"id": entry["id"], "content": entry["content"]}
        )
    writing = await writing_preferences_snapshot(session, user_id)
    return {**groups, **({"writing_preference": writing} if writing else {})}


async def read_system_context(
    user_id: UUID,
    session: AsyncSession,
    conversation: Conversation | None,
) -> SystemContext:
    active_memories = (
        conversation.memory_snapshot_json if conversation is not None else None
    )
    tasks = (
        await current_job_summary(user_id, session, conversation)
        if conversation is not None
        else None
    )
    return SystemContext(active_memories or {}, tasks)


async def agent_system_message(
    agent: AgentDeps, session: AsyncSession, *, summary: Payload | None
) -> ChatCompletionSystemMessageParam:
    conversation = await find_conversation(
        agent.user_id, session, agent.conversation_id
    )
    context = await read_system_context(agent.user_id, session, conversation)
    return context.render(summary)


def render_turn(messages: Iterable[Message], *, current: bool) -> list[Payload]:
    turn: list[Payload] = []
    for message in messages:
        if message.role != MessageRole.TOOL:
            turn.append(
                {
                    "role": message.role,
                    "content": _history_content(message, current=current),
                }
            )
        elif message.steps_json and not message.event_key.startswith("confirm:"):
            turn.append(
                {
                    "role": "assistant",
                    "content": wrap_untrusted_text(
                        "past_step",
                        dump_for_model(
                            [model_view(step) for step in message.steps_json],
                            separators=(",", ":"),
                        ),
                    ),
                }
            )
    return turn


def _render_history(
    rows: list[Message], current_run_id: UUID
) -> tuple[list[Payload], list[HistoryBoundary]]:
    messages: list[Payload] = []
    boundaries: list[HistoryBoundary] = []
    for run_id, group in groupby(rows, key=lambda message: message.run_id):
        turn = list(group)
        current = run_id == current_run_id
        rendered = render_turn(turn, current=current)
        if rendered and not current:
            boundaries.append(
                HistoryBoundary(
                    count=len(rendered), first_seq=turn[0].seq, last_seq=turn[-1].seq
                )
            )
        messages.extend(rendered)
    return messages, boundaries


async def build_context(agent: AgentDeps) -> AgentState:
    """读取本轮可用的对话记录并初始化 Agent 图状态。"""
    limits = settings.agent_limits
    async with read_snapshot(agent.sessions) as session:
        user_id, conversation_id = agent.user_id, agent.conversation_id
        conversation = await find_conversation(user_id, session, conversation_id)
        summary = conversation.summary_json if conversation else None
        through = conversation.summary_through_seq if conversation else 0
        rows = await unsummarized_messages(user_id, session, conversation_id, through)
    messages, boundaries = _render_history(rows, agent.run_id)
    current_user = next(
        (
            row
            for row in rows
            if row.run_id == agent.run_id and row.role == MessageRole.USER
        ),
        None,
    )
    input_clean = bool(
        current_user is not None
        and not current_user.attachments_json
        and len(current_user.content) <= MEMORY_AUTO_MAX_BODY_CHARS
    )
    return {
        "messages": messages,
        "history_boundaries": boundaries,
        "work_summary": summary,
        "run_id": str(agent.run_id),
        "reply": "",
        "calls": [],
        "asked": False,
        "message_used_as_jd": False,
        "input_clean": input_clean,
        "external_context_read": False,
        "clarification": None,
        "action_id": "",
        "switch_job_id": None,
        "budget_exhausted": False,
        "tool_limit_reached": False,
        "generation_results": {},
        "tools_left": limits.tool_calls,
        "seconds_left": agent.seconds_left,
    }
