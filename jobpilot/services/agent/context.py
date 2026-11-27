"""从业务消息重建最近对话，历史工具只携带简短结果。"""

from collections.abc import Iterable, Sequence
from itertools import groupby
from json import dumps
from uuid import UUID

from openai.types.chat import ChatCompletionSystemMessageParam
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.config import settings
from jobpilot.db.conversations import find_conversation
from jobpilot.db.history import unsummarized_messages
from jobpilot.db.models import Conversation, Message, MessageRole
from jobpilot.services.agent.read_tools import current_job_summary
from jobpilot.services.agent.step_details import DETAIL_KEY, model_view, step_detail
from jobpilot.services.agent.types import (
    AgentDeps,
    AgentState,
    HistoryBoundary,
    Payload,
)
from jobpilot.services.agent.work_summary import StoredWorkSummary, render_summary
from jobpilot.services.llm_gateway import redact_pii, wrap_untrusted_text
from jobpilot.services.memories import memory_views
from jobpilot.services.prompts import load_prompt


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


def serialize(value: object) -> str:
    return dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def serialize_tool_result(result: Payload, *, limit: int | None = None) -> str:
    """在字段边界裁剪当轮超量回包，始终交给模型完整的 JSON。"""
    content = serialize(result)
    limit = settings.agent_limits.tool_result_bytes if limit is None else limit
    if len(content.encode()) <= limit:
        return content
    preview: Payload = {"truncated": True}
    field_limit = limit // max(1, len(result))
    for key, value in result.items():
        candidate = {**preview, key: _result_preview(value, field_limit)}
        if len(serialize(candidate).encode()) <= limit:
            preview = candidate
    return serialize(preview)


def summarize_tool(name: str, result: Payload) -> Payload:
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
        "decision",
    )
    summary = {key: result[key] for key in keys if key in result}
    if name == "ask_user" and "answers" in result:
        summary["answers"] = result["answers"]
    detail = step_detail(name, result)
    if detail is not None:
        summary[DETAIL_KEY] = detail
    return {"tool": name, **summary}


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


async def system_message(
    user_id: UUID,
    session: AsyncSession,
    conversation: Conversation | None,
    *,
    summary: Payload | None,
) -> ChatCompletionSystemMessageParam:
    active_memories = {
        item.key: item.value
        for item in await memory_views(session, user_id)
        if item.value is not None
    }
    blocks = [load_prompt("agent").text]
    if active_memories:
        blocks.append(
            "长期记忆\n"
            + wrap_untrusted_text("long_term_memory", serialize(active_memories))
        )
    if summary:
        work = StoredWorkSummary.model_validate(summary)
        blocks.append(
            "工作摘要\n" + wrap_untrusted_text("work_summary", render_summary(work))
        )
    if conversation is not None:
        tasks = await current_job_summary(user_id, session, conversation)
        blocks.append(
            "任务状态\n" + wrap_untrusted_text("task_state", serialize(tasks))
        )
    return {"role": "system", "content": "\n".join(blocks)}


async def agent_system_message(
    agent: AgentDeps, session: AsyncSession, *, summary: Payload | None
) -> ChatCompletionSystemMessageParam:
    conversation = await find_conversation(
        agent.user_id, session, agent.conversation_id
    )
    return await system_message(agent.user_id, session, conversation, summary=summary)


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
                        serialize([model_view(step) for step in message.steps_json]),
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
    async with agent.sessions() as session, session.begin():
        await session.connection(
            execution_options={"isolation_level": "REPEATABLE READ"}
        )
        user_id, conversation_id = agent.user_id, agent.conversation_id
        conversation = await find_conversation(user_id, session, conversation_id)
        summary = conversation.summary_json if conversation else None
        through = conversation.summary_through_seq if conversation else 0
        rows = await unsummarized_messages(user_id, session, conversation_id, through)
        system = await system_message(user_id, session, conversation, summary=summary)
    messages, boundaries = _render_history(rows, agent.run_id)
    return {
        "messages": [system, *messages],
        "history_boundaries": boundaries,
        "work_summary": summary,
        "run_id": str(agent.run_id),
        "reply": "",
        "calls": [],
        "asked": False,
        "message_used_as_jd": False,
        "clarification": None,
        "action_id": "",
        "switch_job_id": None,
        "budget_exhausted": False,
        "generation_results": {},
        "tools_left": limits.tool_calls,
        "seconds_left": agent.seconds_left,
    }
