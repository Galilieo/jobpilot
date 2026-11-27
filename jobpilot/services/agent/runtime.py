"""后台进程执行或恢复图。SSE 订阅者离开时，业务任务继续执行。"""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import replace
from enum import StrEnum
from time import monotonic
from typing import Any
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command, StateSnapshot
from openai import APIConnectionError, AsyncOpenAI
from sqlalchemy import URL
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from jobpilot.config import settings
from jobpilot.db.checkpoints import checkpoint_saver
from jobpilot.db.conversations import (
    add_message,
    conversation_thread,
    find_action,
    find_conversation,
    latest_confirmation,
    owned_agent_run,
    waiting_agent_runs,
)
from jobpilot.db.models import (
    ActionStatus,
    AgentAction,
    AiRun,
    MessageRole,
    RunStatus,
    StopReason,
)
from jobpilot.db.runs import finish_run
from jobpilot.diagnostics import describe_error, error_locations
from jobpilot.services.agent.compaction import (
    CompactConflict,
    CompactSchedule,
    schedule_compaction,
)
from jobpilot.services.agent.context import serialize
from jobpilot.services.agent.graph import build_agent_graph
from jobpilot.services.agent.questions import load_question_card
from jobpilot.services.agent.types import AgentDeps, Payload
from jobpilot.services.agent.window_messages import CurrentInputOverflow
from jobpilot.services.llm_gateway import DailyModelQuotaError

logger = logging.getLogger("uvicorn.error").getChild("agent")
_TIMEOUT_REPLY = "本轮已达到时长上限。已完成的步骤保留在对话中。"
_ERROR_REPLY = "本轮对话未完成。可以查看已完成步骤，或重新发消息。"
# 网关已经按 model_max_attempts 重发过仍连不上，多半是网络或代理，告诉用户该查什么。
_CONNECTION_REPLY = (
    "这一轮没有完成：连不上模型服务（网络超时或中断）。检查网络或代理后重新发送即可。"
)
LaunchTask = Callable[[UUID, Coroutine[Any, Any, None]], None]


class ConfirmationStatus(StrEnum):
    WAITING_DECISION = "waiting_decision"
    DECISION_SAVED = "decision_saved"
    RECOVERING = "recovering"


def confirmation_status(
    run: AiRun,
    card: AgentAction | None,
    recovery_action_id: UUID | None = None,
) -> ConfirmationStatus | None:
    """从已公开的卡和进程中的恢复任务推导状态。"""
    if run.status != RunStatus.WAITING_CONFIRMATION or card is None:
        return None
    if card.status == ActionStatus.PENDING:
        return ConfirmationStatus.WAITING_DECISION
    if card.decision_json is None:
        return None
    # 业务写入后，图仍可能尚未提交下一张卡或最终回复。
    if recovery_action_id == card.action_id:
        return ConfirmationStatus.RECOVERING
    return ConfirmationStatus.DECISION_SAVED


class EventStream:
    """同一进程内重连可旁听当前执行。数据库保存可以跨进程重读的步骤。"""

    def __init__(self, action_id: UUID | None = None) -> None:
        self.action_id = action_id
        self.events: list[tuple[str, Payload]] = []
        self.changed = asyncio.Event()
        self.closed = False

    def emit(self, name: str, event: Payload) -> None:
        self.events.append((name, event))
        self.changed.set()

    def close(self) -> None:
        self.closed = True
        self.changed.set()

    async def iterate(self) -> AsyncIterator[str]:
        offset = 0
        while True:
            while offset < len(self.events):
                name, payload = self.events[offset]
                offset += 1
                yield f"event: {name}\ndata: {serialize(payload)}\n\n"
            if self.closed:
                return
            self.changed.clear()
            try:
                await asyncio.wait_for(self.changed.wait(), timeout=15)
            except TimeoutError:
                yield ": keep-alive\n\n"


def card_payload(action: AgentAction) -> Payload:
    asks_question = action.tool == "ask_user"
    args = (
        load_question_card(action.args_json).model_dump(mode="json")
        if asks_question
        else action.args_json
    )
    return {
        "action_id": str(action.action_id),
        "run_id": str(action.run_id),
        "tool": action.tool,
        "args": args,
        "status": action.status,
        "kind": "question" if asks_question else "confirmation",
    }


def action_result(action: AgentAction) -> Payload:
    return {
        "action_id": str(action.action_id),
        "tool": action.tool,
        "status": ActionStatus.EXECUTED
        if action.status == ActionStatus.UNDONE
        else action.status,
        "result": action.result_json,
    }


def done_event(run: AiRun, action: AgentAction | None = None) -> Payload:
    result = {
        "run_id": str(run.run_id),
        "status": run.status,
        "stop_reason": run.stop_reason,
    }
    if action:
        result["action"] = action_result(action)
    return result


class AgentRuntime:
    """管理可旁听的执行片段，写入的幂等性由数据库保证。"""

    def __init__(self) -> None:
        self.streams: dict[UUID, EventStream] = {}

    def open(self, run_id: UUID, action_id: UUID | None) -> EventStream:
        stream = EventStream(action_id)
        self.streams[run_id] = stream
        return stream

    def start(
        self,
        sessions: async_sessionmaker[AsyncSession],
        client: AsyncOpenAI,
        run: AiRun,
        launch: LaunchTask,
        *,
        action_id: UUID | None = None,
    ) -> EventStream:
        """认领当前执行片段，重复请求订阅已存在的执行。"""
        current = self.streams.get(run.run_id)
        if current is not None and current.action_id == action_id:
            return current
        engine = sessions.kw["bind"]
        if not isinstance(engine, AsyncEngine):
            raise RuntimeError("对话 Agent 需要异步数据库引擎")
        stream = self.open(run.run_id, action_id)
        launch(
            run.run_id,
            self.run(
                sessions,
                engine.url,
                client,
                run,
                stream,
                action_id=action_id,
                launch=launch,
            ),
        )
        return stream

    async def recover_decided_runs(
        self,
        sessions: async_sessionmaker[AsyncSession],
        client: AsyncOpenAI,
        launch: LaunchTask,
    ) -> None:
        """启动时补做已经保存的决定。仍待用户决定的卡保持等待。"""
        async with sessions() as session:
            for run in await waiting_agent_runs(session):
                card = await latest_confirmation(run.user_id, session, run.run_id)
                current = self.streams.get(run.run_id)
                if (
                    confirmation_status(
                        run, card, current.action_id if current else None
                    )
                    == ConfirmationStatus.DECISION_SAVED
                ):
                    self.start(sessions, client, run, launch, action_id=card.action_id)

    async def run(
        self,
        sessions: async_sessionmaker[AsyncSession],
        database_url: URL,
        client: AsyncOpenAI,
        run: AiRun,
        stream: EventStream,
        *,
        action_id: UUID | None = None,
        launch: LaunchTask | None = None,
    ) -> None:
        try:
            finished = await _drive(
                sessions, database_url, client, run, stream, action_id
            )
            if finished and launch is not None:
                await _auto_compact(
                    CompactSchedule(
                        sessions,
                        client,
                        launch,
                        run.user_id,
                        run.conversation_id,
                        automatic=True,
                    )
                )
        except (CurrentInputOverflow, DailyModelQuotaError) as error:
            await _finish(
                sessions, run, stream, RunStatus.FAILED, str(error), action_id
            )
        except TimeoutError:
            await _finish(
                sessions,
                run,
                stream,
                RunStatus.BUDGET_EXHAUSTED,
                _TIMEOUT_REPLY,
                action_id,
                StopReason.BUDGET_EXHAUSTED,
            )
        except Exception as error:
            logger.warning(
                "对话运行 %s 退出（%s），调用栈：%s",
                run.run_id,
                describe_error(error),
                error_locations(error),
            )
            reply = (
                _CONNECTION_REPLY
                if isinstance(error, APIConnectionError)
                else _ERROR_REPLY
            )
            await _finish(sessions, run, stream, RunStatus.FAILED, reply, action_id)
        finally:
            if self.streams.get(run.run_id) is stream:
                self.streams.pop(run.run_id)
            stream.close()


async def _auto_compact(schedule: CompactSchedule) -> None:
    try:
        await schedule_compaction(schedule)
    except CompactConflict:
        return
    except Exception as error:
        logger.warning("自动压缩安排失败（%s）", describe_error(error))


def _graph_config(run: AiRun) -> RunnableConfig:
    limits = settings.agent_limits
    return {
        "configurable": {"thread_id": run.thread_id},
        "recursion_limit": limits.tool_calls * 6 + 12,
    }


def _interrupted_action(snapshot: StateSnapshot) -> UUID | None:
    if not snapshot.interrupts:
        return None
    return UUID(snapshot.interrupts[0].value["action_id"])


async def _drive(
    sessions: async_sessionmaker[AsyncSession],
    database_url: URL,
    client: AsyncOpenAI,
    run: AiRun,
    stream: EventStream,
    action_id: UUID | None,
) -> bool:
    expected_thread = conversation_thread(run.user_id, run.conversation_id)
    if run.thread_id != expected_thread:
        raise ValueError("对话运行的检查点线程不匹配")
    config = _graph_config(run)
    seconds = settings.agent_limits.seconds
    async with checkpoint_saver(database_url) as saver:
        agent = AgentDeps(
            sessions,
            client,
            run.user_id,
            run.conversation_id,
            run.run_id,
            stream.emit,
            monotonic(),
            seconds,
        )
        graph = build_agent_graph(agent, saver)
        has_work = True
        input_state: dict[str, str] | Command | None = {"run_id": str(run.run_id)}
        if action_id is not None:
            snapshot = await graph.aget_state(config)
            if snapshot.values.get("run_id") != str(run.run_id):
                raise ValueError("检查点不属于当前对话运行")
            current_card = _interrupted_action(snapshot)
            if current_card is not None and current_card != action_id:
                await _pause(sessions, run, stream, current_card, action_id)
                return False
            seconds = snapshot.values["seconds_left"]
            agent = replace(agent, started=monotonic(), seconds=seconds)
            graph = build_agent_graph(agent, saver)
            input_state = Command(resume=True) if current_card else None
            has_work = current_card is not None or bool(snapshot.next)
        async with asyncio.timeout(seconds):
            if has_work:
                await graph.ainvoke(input_state, config, durability="sync")
            snapshot = await graph.aget_state(config)
    if interrupted := _interrupted_action(snapshot):
        await _pause(sessions, run, stream, interrupted, action_id)
        return False
    exhausted = snapshot.values.get("budget_exhausted", False)
    await _finish(
        sessions,
        run,
        stream,
        RunStatus.BUDGET_EXHAUSTED if exhausted else RunStatus.SUCCEEDED,
        snapshot.values.get("reply", ""),
        action_id,
        StopReason.BUDGET_EXHAUSTED if exhausted else None,
    )
    return True


async def _pause(
    sessions: async_sessionmaker[AsyncSession],
    original: AiRun,
    stream: EventStream,
    next_action_id: UUID,
    completed_action: UUID | None,
) -> None:
    async with sessions.begin() as session:
        conversation = await find_conversation(
            original.user_id, session, original.conversation_id, lock=True
        )
        run = await owned_agent_run(
            original.user_id,
            session,
            original.conversation_id,
            original.run_id,
            lock=True,
        )
        if (
            conversation is None
            or run is None
            or run.status not in (RunStatus.RUNNING, RunStatus.WAITING_CONFIRMATION)
        ):
            return
        action = await find_action(
            original.user_id,
            session,
            next_action_id,
            conversation_id=original.conversation_id,
        )
        previous = (
            await find_action(original.user_id, session, completed_action)
            if completed_action
            else None
        )
        if action is None or action.status != ActionStatus.PENDING:
            raise ValueError("检查点没有对应的待确认卡")
        card = card_payload(action)
        await add_message(
            session,
            run,
            MessageRole.TOOL,
            f"confirm:{next_action_id}",
            "",
            steps=[{"confirm": card}],
        )
        run.status = RunStatus.WAITING_CONFIRMATION
    stream.emit("confirm", card)
    stream.emit("done", done_event(run, previous))


async def _finish(
    sessions: async_sessionmaker[AsyncSession],
    original: AiRun,
    stream: EventStream,
    status: RunStatus,
    reply: str,
    action_id: UUID | None,
    stop_reason: StopReason | None = None,
) -> None:
    async with sessions.begin() as session:
        run = await owned_agent_run(
            original.user_id,
            session,
            original.conversation_id,
            original.run_id,
            lock=True,
        )
        if run is None or run.status not in (
            RunStatus.RUNNING,
            RunStatus.WAITING_CONFIRMATION,
        ):
            return
        await finish_run(session, run.user_id, run.run_id, status)
        run.stop_reason = stop_reason
        await add_message(session, run, MessageRole.ASSISTANT, "final", reply)
        action = (
            await find_action(run.user_id, session, action_id) if action_id else None
        )
    if status == RunStatus.FAILED:
        stream.emit("error", {"message": reply})
    elif action and action.result_json and "error" in action.result_json:
        stream.emit("error", action.result_json)
    elif status == RunStatus.BUDGET_EXHAUSTED:
        stream.emit("text", {"text": reply})
    stream.emit("done", done_event(run, action))
