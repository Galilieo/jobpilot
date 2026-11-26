"""从业务消息重建最近对话，历史工具只携带简短结果。"""

from collections.abc import Iterable
from itertools import groupby
from json import dumps
from uuid import UUID

from openai.types.chat import ChatCompletionSystemMessageParam
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.config import settings
from jobpilot.db.conversations import find_conversation, recent_messages
from jobpilot.db.models import Message, MessageRole
from jobpilot.services.agent.read_tools import current_job_summary
from jobpilot.services.agent.types import AgentDeps, AgentState, Payload
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


def serialize_tool_result(result: Payload) -> str:
    """在字段边界裁剪当轮超量回包，始终交给模型完整的 JSON。"""
    content = serialize(result)
    limit = settings.agent_limits.tool_result_bytes
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
    return {"tool": name, **summary}


def _history_content(message: Message, *, current: bool) -> str:
    content = message.content
    if message.role == MessageRole.USER and current:
        for index, attachment in enumerate(message.attachments_json, start=1):
            content += "\n" + wrap_untrusted_text(
                f"attachment_{index}", attachment["text"]
            )
    return content


async def agent_system_message(
    agent: AgentDeps, session: AsyncSession
) -> ChatCompletionSystemMessageParam:
    conversation = await find_conversation(
        agent.user_id, session, agent.conversation_id
    )
    job_id = conversation.current_job_id if conversation else None
    summary = await current_job_summary(agent.user_id, session, job_id)
    memories = {
        item.key: item.value
        for item in await memory_views(session, agent.user_id)
        if item.value is not None
    }
    system = load_prompt("agent").text
    if memories:
        system += "\n长期记忆\n" + wrap_untrusted_text(
            "long_term_memory", serialize(memories)
        )
    if summary:
        system += "\n当前岗位摘要\n" + wrap_untrusted_text(
            "current_job", serialize(summary)
        )
    return {"role": "system", "content": system}


def _render_turn(messages: Iterable[Message], *, current: bool) -> list[Payload]:
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
                        "past_step", serialize(message.steps_json)
                    ),
                }
            )
    return turn


def _render_recent_history(
    rows: list[Message], current_run_id: UUID, history_tokens: int
) -> list[Payload]:
    grouped = [
        _render_turn(group, current=run_id == current_run_id)
        for run_id, group in groupby(rows, key=lambda message: message.run_id)
    ]
    # 以 UTF-8 字节数保守估算，沿用模型网关的长度口径。
    while len(grouped) > 1 and len(serialize(grouped).encode()) > history_tokens:
        grouped.pop(0)
    return [message for turn in grouped for message in turn]


async def build_context(agent: AgentDeps) -> AgentState:
    """读取本轮可用的对话记录并初始化 Agent 图状态。"""
    limits = settings.agent_limits
    async with agent.sessions() as session:
        rows = await recent_messages(
            agent.user_id, session, agent.conversation_id, limits.history_turns
        )
        system = await agent_system_message(agent, session)
    return {
        "messages": [
            system,
            *_render_recent_history(rows, agent.run_id, limits.history_tokens),
        ],
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
