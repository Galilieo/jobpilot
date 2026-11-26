"""从业务消息重建最近对话，历史工具只携带简短结果。"""

from itertools import groupby
from json import dumps

from jobpilot.config import settings
from jobpilot.db.conversations import find_conversation, recent_messages
from jobpilot.db.models import Message, MessageRole
from jobpilot.services.agent.read_tools import current_job_summary
from jobpilot.services.agent.types import AgentDeps, AgentState, Payload
from jobpilot.services.llm_gateway import wrap_untrusted_text
from jobpilot.services.prompts import load_prompt


def serialize(value: object) -> str:
    return dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


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
        for attachment in message.attachments_json:
            content += "\n" + wrap_untrusted_text("attachment", attachment["text"])
    return content


async def build_context(agent: AgentDeps) -> AgentState:
    limits = settings.agent_limits
    async with agent.sessions() as session:
        rows = await recent_messages(
            agent.user_id, session, agent.conversation_id, limits.history_turns
        )
        conversation = await find_conversation(
            agent.user_id, session, agent.conversation_id
        )
        job_id = conversation.current_job_id if conversation else None
        summary = await current_job_summary(agent.user_id, session, job_id)
    prompt = load_prompt("agent")
    system = prompt.text
    if summary:
        system += "\n当前岗位摘要\n" + wrap_untrusted_text(
            "current_job", serialize(summary)
        )
    grouped = []
    for run_id, group in groupby(rows, key=lambda message: message.run_id):
        current = run_id == agent.run_id
        turn = []
        for message in group:
            if message.role == MessageRole.TOOL:
                if message.steps_json and not message.event_key.startswith("confirm:"):
                    turn.append(
                        {
                            "role": "assistant",
                            "content": wrap_untrusted_text(
                                "past_step", serialize(message.steps_json)
                            ),
                        }
                    )
            else:
                turn.append(
                    {
                        "role": message.role,
                        "content": _history_content(message, current=current),
                    }
                )
        grouped.append(turn)
    # 以 UTF-8 字节数保守估算，沿用模型网关的长度口径。
    while len(grouped) > 1 and len(serialize(grouped).encode()) > limits.history_tokens:
        grouped.pop(0)
    return {
        "messages": [
            {"role": "system", "content": system},
            *[item for turn in grouped for item in turn],
        ],
        "run_id": str(agent.run_id),
        "reply": "",
        "calls": [],
        "asked": False,
        "action_id": "",
        "switch_job_id": None,
        "budget_exhausted": False,
        "tools_left": limits.tool_calls,
        "seconds_left": agent.seconds_left,
    }
