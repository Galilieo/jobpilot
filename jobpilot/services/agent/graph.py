"""用 LangGraph 分离模型、工具提议、等待决定和执行节点。"""

from json import dumps, loads
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt
from openai import APIError
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.config import ModelPurpose, settings
from jobpilot.db.accounts import lock_active_account
from jobpilot.db.conversations import (
    add_message,
    agent_input,
    find_action,
    find_conversation,
    find_tool_action,
    owned_agent_run,
    propose_action,
    set_job_binding,
)
from jobpilot.db.memories import count_record_actions
from jobpilot.db.models import (
    ActionStatus,
    AgentAction,
    AiRun,
    Conversation,
    MessageRole,
    RunStatus,
)
from jobpilot.db.session import read_snapshot
from jobpilot.services.agent.actions import apply_write, bind_job, lock_agent_job
from jobpilot.services.agent.arguments import JobArgs, ParseJobArgs
from jobpilot.services.agent.context import (
    agent_system_message,
    build_context,
    serialize,
    serialize_tool_result,
    summarize_tool_call,
)
from jobpilot.services.agent.history import HistoryArgs
from jobpilot.services.agent.memory_actions import RECORD_TOOLS, prepare_memory
from jobpilot.services.agent.questions import (
    JOB_CHOICE_QUESTION,
    JobSelection,
    OrdinaryQuestions,
    load_question_card,
    number_questions,
    question_results,
)
from jobpilot.services.agent.tools import TOOL_DEFINITIONS, TOOLS, Tool
from jobpilot.services.agent.types import (
    AgentDeps,
    AgentState,
    JobChoice,
    Payload,
    ToolArgs,
    ToolCall,
    ToolCategory,
    ToolContext,
    ToolError,
)
from jobpilot.services.agent.window_messages import (
    AgentRequest,
    AgentWindow,
    CurrentInputOverflow,
)
from jobpilot.services.facts import TruncatedOutputError
from jobpilot.services.llm_gateway import (
    AgentModelBudget,
    BudgetExhaustedError,
    ContextOverflowError,
    EmptyModelResponseError,
    request_completion,
)
from jobpilot.services.matching import MatchValidationError
from jobpilot.services.prompts import load_prompt
from jobpilot.services.request_window import InputTokenHistory, estimate_request_tokens
from jobpilot.services.resume_versions import ResumeNotFoundError
from jobpilot.services.tailoring_store import TailorInputError

_BUDGET_REPLY = "本轮额度已用完。已完成的步骤保留在对话中，可以发消息继续。"
_TOOL_FAILURES = (
    APIError,
    EmptyModelResponseError,
    ValidationError,
    TruncatedOutputError,
    MatchValidationError,
    TailorInputError,
)


_TOOL_LIMIT_RESULT = {
    "status": "tool_limit_reached",
    "error": "本轮工具调用次数已用完，这个调用没有执行。不要再调用工具，"
    "根据已经拿到的信息直接回答；没查全的地方如实告诉用户。",
}


def _exhausted() -> AgentState:
    return {"reply": _BUDGET_REPLY, "budget_exhausted": True, "next_tool": "finish"}


def _answer_without_tools(state: AgentState) -> AgentState:
    """工具次数用完时不丢掉已查到的结果，让模型再答一次。

    未执行的调用也要回一条工具消息，否则请求不符合工具调用协议。
    模型仍要调用工具时才停下并给出固定提示，避免循环。
    """
    if state.get("tool_limit_reached"):
        return _exhausted()
    skipped = [
        {
            "role": "tool",
            "tool_call_id": call["call_id"],
            "content": serialize(_TOOL_LIMIT_RESULT),
        }
        for call in state["calls"]
    ]
    return {
        "messages": [*state["messages"], *skipped],
        "calls": [],
        "tool_limit_reached": True,
        "next_tool": "call_model",
    }


def _model_message(
    completion: ChatCompletion, turn: int
) -> tuple[Payload, list[ToolCall]]:
    message = completion.choices[0].message
    functions = [call for call in message.tool_calls or [] if call.type == "function"]
    # 把问题排到最后，合并同次模型输出中的提问，同时让工具回包顺序对应。
    functions.sort(key=lambda call: call.function.name == "ask_user")
    calls: list[ToolCall] = [
        {
            "call_id": call.id,
            "name": call.function.name,
            "arguments": call.function.arguments,
            "key": f"{turn}:{index}:{call.id}",
        }
        for index, call in enumerate(functions)
    ]
    output = {"role": "assistant", "content": message.content}
    if functions and (reasoning := getattr(message, "reasoning_content", None)):
        output["reasoning_content"] = reasoning
    if functions:
        output["tool_calls"] = [
            call.model_dump(include={"id", "type", "function"}) for call in functions
        ]
    return output, calls


def _validate_completion(completion: ChatCompletion) -> None:
    choice = completion.choices[0]
    message = choice.message
    ids = [call.id for call in message.tool_calls or []]
    if len(ids) != len(set(ids)) or (not ids and not message.content):
        raise EmptyModelResponseError("对话模型未返回有效回复或工具调用")


def _uses_body_as_jd(call: ToolCall) -> bool:
    if call["name"] != "parse_jd":
        return False
    try:
        return ParseJobArgs.model_validate_json(call["arguments"]).source == "message"
    except ValidationError:
        return False


def _shrink_tool_results(
    state: AgentState,
    messages: list[ChatCompletionMessageParam],
) -> list[ChatCompletionMessageParam]:
    prior_end = 1 + sum(item["count"] for item in state.get("history_boundaries", []))
    adapted = list(messages)
    limit = max(128, settings.agent_limits.tool_result_bytes // 8)
    for index in range(prior_end, len(adapted)):
        message = adapted[index]
        if message["role"] == "tool" and isinstance(message["content"], str):
            adapted[index] = {
                **message,
                "content": serialize_tool_result(
                    loads(message["content"]), limit=limit
                ),
            }
    return adapted


def _request_for(
    state: AgentState,
    messages: list[ChatCompletionMessageParam],
    window: AgentWindow,
    *,
    smaller_than: int | None = None,
) -> AgentRequest | None:
    history = state.get("history_boundaries", [])
    request = window.request(messages, history, smaller_than=smaller_than)
    if request is not None:
        return request
    trimmed = _shrink_tool_results(state, messages)
    return window.request(trimmed, history, smaller_than=smaller_than)


async def _send_main(
    agent: AgentDeps,
    state: AgentState,
    messages: list[ChatCompletionMessageParam],
    window: AgentWindow,
) -> ChatCompletion:
    request = _request_for(state, messages, window)
    if request is None:
        raise CurrentInputOverflow("本轮输入超出模型窗口，请拆分消息或附件再发送。")
    prompt = load_prompt("agent")
    emitted = False

    def emit(text: str) -> None:
        nonlocal emitted
        emitted = True
        agent.emit("text", {"text": text})

    async def send(outbound: AgentRequest) -> ChatCompletion:
        return await request_completion(
            agent.sessions,
            agent.user_id,
            agent.run_id,
            client=agent.client,
            purpose=ModelPurpose.AGENT,
            prompt_version=prompt.label,
            messages=outbound.messages,
            json_output=False,
            budget=AgentModelBudget(agent.run_id),
            validate=_validate_completion,
            tools=TOOL_DEFINITIONS,
            on_text=emit,
            max_output_tokens=outbound.output_tokens,
        )

    try:
        completion = await send(request)
    except ContextOverflowError:
        window.tokens.clear()
        if emitted:
            raise
        rejected_size = estimate_request_tokens(
            window.outbound(request.messages), TOOL_DEFINITIONS
        )
        retry = _request_for(state, messages, window, smaller_than=rejected_size)
        if retry is None:
            raise CurrentInputOverflow(
                "模型窗口不足，请拆分本轮消息或附件再发送。"
            ) from None
        request = retry
        completion = await send(retry)
    usage = completion.usage
    window.record(request, usage.prompt_tokens if usage is not None else None)
    return completion


async def _call_model(
    agent: AgentDeps, state: AgentState, window: AgentWindow
) -> AgentState:
    async with read_snapshot(agent.sessions) as session:
        system = await agent_system_message(
            agent, session, summary=state.get("work_summary")
        )
    history = state["messages"]
    # 老检查点和后续工具轮次会带上一份系统消息，主调用前替换为最新状态。
    if history and history[0]["role"] == "system":
        history = history[1:]
    messages = [system, *history]
    try:
        completion = await _send_main(agent, state, messages, window)
    except BudgetExhaustedError:
        window.tokens.clear()
        return {"seconds_left": agent.seconds_left, **_exhausted()}
    except Exception:
        window.tokens.clear()
        raise
    output, calls = _model_message(completion, len(messages))
    return {
        "seconds_left": agent.seconds_left,
        "messages": [*messages, output],
        "calls": calls,
        "message_used_as_jd": state.get("message_used_as_jd", False)
        or any(_uses_body_as_jd(call) for call in calls),
        "reply": output["content"] or "",
        "next_tool": "select_tool" if calls else "finish",
    }


_EXTERNAL_CONTEXT_TOOLS = frozenset({"parse_jd", "read_job", "search_conversations"})


def _reads_external_context(agent: AgentDeps, call: ToolCall) -> bool:
    if call["name"] in _EXTERNAL_CONTEXT_TOOLS:
        return True
    if call["name"] != "read_conversation_history":
        return False
    try:
        args = TOOLS["read_conversation_history"].parse_arguments(call["arguments"])
    except ToolError:
        return False
    return (
        isinstance(args, HistoryArgs)
        and args.conversation_id is not None
        and args.conversation_id != agent.conversation_id
    )


def _needs_memory_confirmation(state: AgentState, name: str) -> bool:
    if name not in RECORD_TOOLS:
        return False
    if name == "forget":
        return True
    return not state.get("input_clean", False) or state.get(
        "external_context_read", False
    )


def _select_tool(state: AgentState) -> AgentState:
    if not state["calls"]:
        return {"next_tool": "call_model"}
    if state["tools_left"] < 1:
        return _answer_without_tools(state)
    tool = TOOLS.get(state["calls"][0]["name"])
    if tool is not None and (
        tool.category in (ToolCategory.WRITE, ToolCategory.ASK)
        or _needs_memory_confirmation(state, tool.name)
    ):
        return {"next_tool": "propose_action"}
    return {"next_tool": "execute_tool"}


async def _owned_run(
    agent: AgentDeps, session: AsyncSession, *, lock: bool = False
) -> AiRun:
    run = await owned_agent_run(
        agent.user_id, session, agent.conversation_id, agent.run_id, lock=lock
    )
    if run is None or run.status not in (
        RunStatus.RUNNING,
        RunStatus.WAITING_CONFIRMATION,
    ):
        raise ToolError("对话运行已结束", 409)
    return run


async def _record(agent: AgentDeps, call: ToolCall, result: Payload) -> None:
    summary = summarize_tool_call(call, result)
    async with agent.sessions.begin() as session:
        run = await _owned_run(agent, session, lock=True)
        await add_message(
            session, run, MessageRole.TOOL, "tool:" + call["key"], "", steps=[summary]
        )
    agent.emit("tool_end", summarize_tool_call(call, result, live=True))


def _tool_message(call: ToolCall, result: Payload) -> Payload:
    # 整理空间需要保留每个条目的 ID。只有窗口不足时才按通用规则裁剪。
    full_result = call["name"] in RECORD_TOOLS and "entries" in result
    return {
        "role": "tool",
        "tool_call_id": call["call_id"],
        "content": serialize(result) if full_result else serialize_tool_result(result),
    }


def _tool_start(agent: AgentDeps, call: ToolCall) -> None:
    agent.emit("tool_start", {"tool": call["name"], "tool_call_id": call["call_id"]})


async def _result_for(
    agent: AgentDeps,
    call: ToolCall,
    args: ToolArgs | None = None,
    *,
    message_used_as_jd: bool = False,
) -> Payload:
    tool = TOOLS.get(call["name"])
    if tool is None:
        return {"status": 422, "error": "未知工具"}
    try:
        context = ToolContext(agent, call, message_used_as_jd=message_used_as_jd)
        if args is not None:
            return await tool.execute(context, args)
        return await tool.invoke(context)
    except ToolError as error:
        return {"status": error.status, "error": str(error)}
    except ResumeNotFoundError:
        return {"status": 404, "error": "定制简历不存在"}
    except _TOOL_FAILURES:
        return {"status": 502, "error": "工具没有完成，请稍后重试"}


async def _generation_limit(agent: AgentDeps, name: str) -> int:
    limits = settings.agent_limits
    if name == "parse_jd":
        async with agent.sessions() as db:
            message = await agent_input(agent.user_id, db, agent.run_id)
        attachment_count = len(message.attachments_json) if message else 0
        return max(attachment_count, limits.parse_jd_without_attachments)
    return {
        "match_job": limits.match_job_calls,
        "tailor_resume": limits.tailor_resume_calls,
    }[name]


async def _generate_once(
    agent: AgentDeps, state: AgentState, call: ToolCall, tool: Tool
) -> tuple[Payload, AgentState]:
    """按校验后的参数复用结果，只有不同输入才占用生成次数。"""
    try:
        args = tool.parse_arguments(call["arguments"])
    except ToolError as error:
        return {"status": error.status, "error": str(error)}, {}
    if isinstance(args, ParseJobArgs) and args.source == "message":
        async with agent.sessions() as db:
            if await count_record_actions(db, agent.user_id, agent.run_id):
                return {
                    "status": 409,
                    "error": "本轮正文已用于记忆。请单独发送 JD 或使用附件。",
                }, {}
    if isinstance(args, JobArgs) and args.job_id is None:
        # 默认岗位会随确认卡切换。缓存和执行必须使用同一个实际岗位 ID。
        async with agent.sessions() as db:
            conversation = await find_conversation(
                agent.user_id, db, agent.conversation_id
            )
        if conversation is not None:
            args = args.model_copy(update={"job_id": conversation.current_job_id})
    cache = state.get("generation_results", {})
    prior = cache.get(tool.name, {})
    key = dumps(args.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    if key in prior:
        return prior[key], {}
    if len(prior) >= await _generation_limit(agent, tool.name):
        return {"status": 422, "error": "本轮已达到该生成工具的调用上限"}, {}
    result = await _result_for(agent, call, args)
    return result, {"generation_results": {**cache, tool.name: {**prior, key: result}}}


async def _bind_parsed_job(agent: AgentDeps, job_id: UUID) -> bool:
    async with agent.sessions.begin() as session:
        conversation = await find_conversation(
            agent.user_id, session, agent.conversation_id, lock=True
        )
        if conversation is None:
            return False
        try:
            if not await lock_agent_job(session, agent.user_id, job_id):
                return False
        except ToolError:
            return False
        if conversation.current_job_id is None:
            set_job_binding(conversation, job_id)
        return conversation.current_job_id != job_id


def _question_call(name: str, turn: int, question: str) -> tuple[Payload, ToolCall]:
    tool_call_id = f"{name}-job"
    arguments = dumps(
        {"questions": [question]},
        ensure_ascii=False,
    )
    output = {
        "role": "assistant",
        "tool_calls": [
            {
                "id": tool_call_id,
                "type": "function",
                "function": {"name": "ask_user", "arguments": arguments},
            }
        ],
    }
    return output, ToolCall(
        call_id=tool_call_id,
        key=f"{name}:{turn}",
        name="ask_user",
        arguments=arguments,
    )


async def _execute_tool(agent: AgentDeps, state: AgentState) -> AgentState:
    call, *remaining = state["calls"]
    _tool_start(agent, call)
    tool = TOOLS.get(call["name"])
    try:
        if tool and tool.category == ToolCategory.GENERATE:
            result, tool_update = await _generate_once(agent, state, call, tool)
        else:
            result, tool_update = (
                await _result_for(
                    agent,
                    call,
                    message_used_as_jd=state.get("message_used_as_jd", False),
                ),
                {},
            )
    except BudgetExhaustedError:
        await _record(agent, call, {"status": "budget_exhausted"})
        return {"seconds_left": agent.seconds_left, **_exhausted()}
    if call["name"] in RECORD_TOOLS and "action_id" in result:
        # 自动记录的成功步骤已和改动一起落库。
        agent.emit("tool_end", summarize_tool_call(call, result))
    else:
        await _record(agent, call, result)
    messages = [*state["messages"], _tool_message(call, result)]
    updates: AgentState = {
        **tool_update,
        "seconds_left": agent.seconds_left,
        "messages": messages,
        "calls": remaining,
        "tools_left": state["tools_left"] - 1,
        "next_tool": "select_tool",
    }
    if result.get("status") == "budget_exhausted":
        return {**updates, **_exhausted()}
    if _reads_external_context(agent, call) and "error" not in result:
        updates["external_context_read"] = True
    if call["name"] == "parse_jd" and tool_update and (job_id := result.get("job_id")):
        if await _bind_parsed_job(agent, UUID(job_id)):
            updates["switch_job_id"] = job_id
    return updates


def _route_tool(state: AgentState) -> str:
    return state["next_tool"]


def _next_call(state: AgentState) -> AgentState:
    if state["calls"] or state["asked"]:
        return _select_tool(state)
    if state.get("switch_job_id"):
        name, question = "switch", JOB_CHOICE_QUESTION
    elif state.get("clarification"):
        name, question = "clarify", state["clarification"]
    else:
        return _select_tool(state)
    if state["tools_left"] < 1:
        return _exhausted()
    output, call = _question_call(name, len(state["messages"]), question)
    return {
        "messages": [*state["messages"], output],
        "calls": [call],
        "next_tool": "propose_action",
    }


async def _ask_arguments(agent: AgentDeps, state: AgentState) -> Payload:
    if job_id := state.get("switch_job_id"):
        return JobSelection(job_id=UUID(job_id)).model_dump(mode="json")
    questions = []
    for call in state["calls"][: state["tools_left"]]:
        result = await _result_for(agent, call)
        questions.extend(result.get("questions", []))
    if not questions:
        return {"error": "提问参数无效", "status": 422}
    return number_questions(list(dict.fromkeys(questions))[:10]).model_dump(mode="json")


async def _propose(agent: AgentDeps, state: AgentState) -> AgentState:
    call = state["calls"][0]
    async with agent.sessions() as session:
        saved = await find_tool_action(
            session, agent.user_id, agent.run_id, call["key"]
        )
    if saved is not None:
        return _wait_for_card(agent, saved)
    _tool_start(agent, call)
    ask = call["name"] == "ask_user"
    try:
        if ask:
            args = (
                {"error": "本轮已经询问过用户"}
                if state["asked"]
                else await _ask_arguments(agent, state)
            )
        elif call["name"] in RECORD_TOOLS:
            parsed_args = TOOLS[call["name"]].parse_arguments(call["arguments"])
            args = await prepare_memory(
                ToolContext(
                    agent,
                    call,
                    message_used_as_jd=state.get("message_used_as_jd", False),
                ),
                parsed_args,
            )
        else:
            args = await _result_for(
                agent, call, message_used_as_jd=state.get("message_used_as_jd", False)
            )
    except ToolError as error:
        args = {"status": error.status, "error": str(error)}
    except BudgetExhaustedError:
        await _record(agent, call, {"status": "budget_exhausted"})
        return {"seconds_left": agent.seconds_left, **_exhausted()}
    if "error" in args or args.get("status") == "exists":
        await _record(agent, call, args)
        return {
            "seconds_left": agent.seconds_left,
            "next_tool": "select_tool",
            "clarification": state.get("clarification") or args.get("clarify"),
            "calls": state["calls"][1:],
            "tools_left": state["tools_left"] - 1,
            "messages": [*state["messages"], _tool_message(call, args)],
        }
    async with agent.sessions.begin() as session:
        run = await _owned_run(agent, session, lock=True)
        action = await propose_action(session, run, call["name"], call["key"], args)
    return _wait_for_card(agent, action)


def _wait_for_card(agent: AgentDeps, action: AgentAction) -> AgentState:
    return {
        "seconds_left": agent.seconds_left,
        "next_tool": "await_confirmation",
        "action_id": str(action.action_id),
    }


def _await_confirmation(state: AgentState) -> AgentState:
    # 恢复时这个节点会从头重跑，决定和参数都由下一节点从数据库读取。
    interrupt({"action_id": state["action_id"]})
    return {}


async def _accept_answer(
    session: AsyncSession, action: AgentAction, conversation: Conversation
) -> Payload:
    card = load_question_card(action.args_json)
    if isinstance(card, OrdinaryQuestions):
        return question_results(card, action.decision_json)
    choice = action.decision_json.get("job_choice")
    if choice not in card.choices:
        return {"error": "请重新选择岗位操作", "status": 422}
    job_id = card.job_id
    if choice == JobChoice.KEEP:
        return {"decision": choice, "job_id": str(job_id)}
    try:
        if not await lock_agent_job(session, action.user_id, job_id):
            return {"error": "新岗位已删除", "status": 404}
    except ToolError as error:
        return {"error": str(error), "status": error.status}
    if choice == JobChoice.SWITCH:
        action.undo_json = bind_job(conversation, job_id).model_dump(mode="json")
    else:
        conversation = Conversation(
            user_id=action.user_id, title="新岗位对话", current_job_id=job_id
        )
        session.add(conversation)
        await session.flush()
    return {
        "decision": choice,
        "job_id": str(job_id),
        "conversation_id": str(conversation.conversation_id),
    }


async def _apply(agent: AgentDeps, state: AgentState) -> AgentState:
    call = state["calls"][0]
    ask = call["name"] == "ask_user"
    calls = state["calls"][: state["tools_left"]] if ask else [call]
    async with agent.sessions.begin() as session:
        if not await lock_active_account(
            session, agent.user_id, exclusive=call["name"] in RECORD_TOOLS
        ):
            raise ToolError("账号当前无法执行对话操作", 403)
        conversation = await find_conversation(
            agent.user_id, session, agent.conversation_id, lock=True
        )
        if conversation is None:
            raise ToolError("对话不存在", 404)
        await _owned_run(agent, session, lock=True)
        action = await find_action(
            agent.user_id,
            session,
            UUID(state["action_id"]),
            conversation_id=agent.conversation_id,
            lock=True,
        )
        if action is None or action.status == ActionStatus.PENDING:
            raise ToolError("确认卡尚未决定", 409)
        if action.result_json is not None:
            result = action.result_json
        elif action.status == ActionStatus.REJECTED:
            result = action.result_json = {"decision": "rejected"}
        elif ask:
            result = action.result_json = await _accept_answer(
                session, action, conversation
            )
            action.status = (
                ActionStatus.FAILED if "error" in result else ActionStatus.EXECUTED
            )
        else:
            result = await apply_write(session, action)
    replies = []
    for item in calls:
        item_result = _question_tool_result(item, result) if ask else result
        await _record(agent, item, item_result)
        replies.append(_tool_message(item, item_result))
    return {
        "messages": [*state["messages"], *replies],
        "calls": state["calls"][len(calls) :],
        "tools_left": state["tools_left"] - len(calls),
        "asked": state["asked"] or ask,
        "switch_job_id": None if ask else state.get("switch_job_id"),
        "action_id": "",
        "seconds_left": agent.seconds_left,
    }


def _question_tool_result(call: ToolCall, result: Payload) -> Payload:
    try:
        TOOLS["ask_user"].parse_arguments(call["arguments"])
    except ToolError as error:
        return {"status": error.status, "error": str(error)}
    return result


def build_agent_graph(
    agent: AgentDeps, saver: BaseCheckpointSaver
) -> CompiledStateGraph:
    """同一对话线程内以新运行的状态覆盖上一轮，恢复时只沿已有检查点继续。"""

    async def context(_state: AgentState) -> AgentState:
        return await build_context(agent)

    window = AgentWindow(InputTokenHistory(), TOOL_DEFINITIONS)

    async def model(state: AgentState) -> AgentState:
        return await _call_model(agent, state, window)

    async def execute(state: AgentState) -> AgentState:
        return await _execute_tool(agent, state)

    async def propose(state: AgentState) -> AgentState:
        return await _propose(agent, state)

    async def apply(state: AgentState) -> AgentState:
        return await _apply(agent, state)

    graph = StateGraph(AgentState)
    graph.add_node("build_context", context)
    graph.add_node("call_model", model)
    graph.add_node("select_tool", _next_call)
    graph.add_node("execute_tool", execute)
    graph.add_node("propose_action", propose)
    graph.add_node("await_confirmation", _await_confirmation)
    graph.add_node("apply_action", apply)
    graph.add_edge(START, "build_context")
    graph.add_edge("build_context", "call_model")
    graph.add_conditional_edges("call_model", _route_tool, ["select_tool", "finish"])
    graph.add_conditional_edges(
        "select_tool",
        _route_tool,
        ["call_model", "execute_tool", "propose_action", "finish"],
    )
    graph.add_conditional_edges("execute_tool", _route_tool, ["select_tool", "finish"])
    graph.add_conditional_edges(
        "propose_action", _route_tool, ["select_tool", "await_confirmation", "finish"]
    )
    graph.add_edge("await_confirmation", "apply_action")
    graph.add_edge("apply_action", "select_tool")
    graph.add_node("finish", lambda _state: {})
    graph.add_edge("finish", END)
    return graph.compile(checkpointer=saver)
