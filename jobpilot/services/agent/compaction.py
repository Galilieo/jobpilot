"""安排对话压缩，固定边界后在事务之外调用模型；也给出当前上下文用量，和自动压缩用同一套估算。"""

import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.config import ModelPurpose, settings
from jobpilot.db.accounts import lock_active_account
from jobpilot.db.compactions import (
    active_compaction,
    advance_summary,
    completed_messages,
    lock_compaction,
    recent_compactions,
)
from jobpilot.db.conversations import find_conversation, latest_agent_run
from jobpilot.db.models import AiRun, Conversation, RunStatus
from jobpilot.db.runs import finish_run
from jobpilot.diagnostics import describe_error
from jobpilot.services.agent.compact_input import (
    CompactBatch,
    CompletedTurn,
    group_turns,
    select_batch,
)
from jobpilot.services.agent.compact_sources import (
    batch_answers,
    verified_actions,
    verified_sources,
)
from jobpilot.services.agent.context import render_turn, system_message
from jobpilot.services.agent.tools import TOOL_DEFINITIONS
from jobpilot.services.agent.types import Payload
from jobpilot.services.agent.work_summary import (
    AnsweredQuestion,
    StoredWorkSummary,
    WorkSummary,
    checked_summary,
    fit_summary,
)
from jobpilot.services.facts import TruncatedOutputError
from jobpilot.services.llm_gateway import (
    CompactModelBudget,
    ContextOverflowError,
    request_completion,
    request_messages_for_model,
)
from jobpilot.services.prompts import load_prompt
from jobpilot.services.request_window import estimate_request_tokens

logger = logging.getLogger("uvicorn.error").getChild("compact")
MAX_FAILURES_AT_BOUNDARY = 3
LaunchCompact = Callable[[UUID, Coroutine[Any, Any, None]], None]


class CompactConflict(ValueError):
    """手动压缩与活跃对话冲突，或账号/对话已不可用。"""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


class CompactResult(BaseModel):
    run_id: UUID | None = None
    status: str
    focus_applied: bool = False


@dataclass(frozen=True)
class CompactWork:
    user_id: UUID
    conversation_id: UUID
    run_id: UUID
    revision: int
    previous_seq: int
    batch: CompactBatch
    answers: list[AnsweredQuestion]
    summary_bytes: int
    input_tokens: int

    @property
    def through_seq(self) -> int:
        return self.batch.turns[-1].end_seq


@dataclass(frozen=True)
class CompactSchedule:
    sessions: async_sessionmaker[AsyncSession]
    client: AsyncOpenAI
    launch: LaunchCompact
    user_id: UUID
    conversation_id: UUID
    focus: str | None = None
    automatic: bool = False


def _output_tokens(purpose: ModelPurpose) -> int:
    return min(
        settings.model_for_purpose(purpose).max_tokens,
        settings.model_profile(purpose).max_output_tokens,
    )


def _compact_input_limit() -> int:
    return min(
        settings.agent_limits.compact_input_tokens,
        settings.effective_window(ModelPurpose.COMPACT)
        - _output_tokens(ModelPurpose.COMPACT),
    )


def _parse_summary(response: ChatCompletion) -> WorkSummary:
    choice = response.choices[0]
    if choice.finish_reason == "length":
        raise TruncatedOutputError("工作摘要被截断")
    return WorkSummary.model_validate_json(choice.message.content or "")


def _failed_attempts(runs: list[AiRun], revision: int) -> list[dict]:
    attempts: list[dict] = []
    for run in runs:
        plan = run.plan_json or {}
        if run.status != RunStatus.FAILED or plan.get("revision") != revision:
            break
        attempts.append(plan)
    return attempts


def _allowed_input(
    schedule: CompactSchedule, candidates: list[CompletedTurn], failures: list[dict]
) -> int:
    limit = _compact_input_limit()
    if not failures:
        return limit
    latest = failures[0]
    if (
        schedule.automatic
        and len(failures) >= MAX_FAILURES_AT_BOUNDARY
        and candidates[-1].end_seq <= latest.get("latest_seq", 0)
    ):
        return 0
    overflow = next((attempt for attempt in failures if attempt.get("overflow")), None)
    return min(limit, overflow["input_size"] // 2) if overflow else limit


async def _create_work(
    schedule: CompactSchedule,
    session: AsyncSession,
    conversation: Conversation,
    candidates: list[CompletedTurn],
    summary_bytes: int,
) -> CompactWork | None:
    revision, previous_seq = (
        conversation.summary_revision,
        conversation.summary_through_seq,
    )
    recent = await recent_compactions(
        schedule.user_id, session, schedule.conversation_id, MAX_FAILURES_AT_BOUNDARY
    )
    failures = _failed_attempts(recent, revision)
    input_tokens = _allowed_input(schedule, candidates, failures)
    if input_tokens <= 0:
        logger.info(
            "暂停对话 %s 在修订号 %s 的自动压缩", schedule.conversation_id, revision
        )
        return None
    previous = fit_summary(
        StoredWorkSummary.model_validate(conversation.summary_json or {}),
        input_tokens // 2,
        previous_seq,
    )
    by_run = await batch_answers(
        schedule.user_id,
        session,
        schedule.conversation_id,
        {t.run_id for t in candidates},
    )
    batch = select_batch(
        previous,
        candidates,
        by_run,
        focus=schedule.focus,
        input_limit=input_tokens,
    )
    if batch is None:
        logger.info("对话 %s 的压缩输入已无可缩小的批次", schedule.conversation_id)
        return None
    run = AiRun(
        user_id=schedule.user_id,
        conversation_id=schedule.conversation_id,
        kind="compact",
        plan_json={
            "revision": revision,
            "latest_seq": candidates[-1].end_seq,
            "input_size": batch.input_size,
        },
    )
    session.add(run)
    await session.flush()
    return CompactWork(
        schedule.user_id,
        schedule.conversation_id,
        run.run_id,
        revision,
        previous_seq,
        batch,
        [answer for turn in batch.turns for answer in by_run.get(turn.run_id, [])],
        summary_bytes,
        input_tokens,
    )


async def _schedule(
    schedule: CompactSchedule, session: AsyncSession
) -> CompactResult | CompactWork:
    user_id, conversation_id = schedule.user_id, schedule.conversation_id
    if not await lock_active_account(session, user_id):
        raise CompactConflict("账号当前无法执行压缩", 403)
    conversation = await find_conversation(user_id, session, conversation_id, lock=True)
    if conversation is None:
        raise CompactConflict("对话不存在", 404)
    existing = await active_compaction(user_id, session, conversation_id)
    if existing is not None:
        return CompactResult(run_id=existing.run_id, status=existing.status)
    agent = await latest_agent_run(user_id, session, conversation_id, active_only=True)
    if agent is not None:
        raise CompactConflict("对话正在运行，请在本轮结束后压缩", 409)
    turns = group_turns(
        await completed_messages(
            user_id,
            session,
            conversation_id,
            conversation.summary_through_seq,
        )
    )
    # 压缩后仍保留最新一轮。自动入口还要检查水位，再计算应该移出的轮次。
    candidates, summary_bytes = await _compaction_candidates(
        schedule, session, conversation, turns
    )
    if not candidates:
        return CompactResult(status="not_needed")
    work = await _create_work(
        schedule, session, conversation, candidates, summary_bytes
    )
    return work or CompactResult(status="not_needed")


async def _projected_request_tokens(
    user_id: UUID,
    session: AsyncSession,
    conversation: Conversation,
    history: list[list[Payload]],
) -> int:
    """下一次主调用的输入估算：当前摘要、摘要之后的完整轮次和工具定义。"""
    current_system = await system_message(
        user_id, session, conversation, summary=conversation.summary_json
    )
    context = [current_system, *(item for turn in history for item in turn)]
    return estimate_request_tokens(
        request_messages_for_model(context), TOOL_DEFINITIONS
    )


class ContextUsage(BaseModel):
    used_tokens: int
    window_tokens: int
    # 估算的输入超过这条线，本轮结束后会自动压缩（触发水位减去预留的输出）。
    compact_at_tokens: int


async def context_usage(
    user_id: UUID, session: AsyncSession, conversation: Conversation
) -> ContextUsage:
    turns = group_turns(
        await completed_messages(
            user_id,
            session,
            conversation.conversation_id,
            conversation.summary_through_seq,
        )
    )
    history = [render_turn(turn.messages, current=False) for turn in turns]
    window = settings.effective_window(ModelPurpose.AGENT)
    trigger = int(window * settings.agent_limits.compact_trigger_ratio)
    return ContextUsage(
        used_tokens=await _projected_request_tokens(
            user_id, session, conversation, history
        ),
        window_tokens=window,
        compact_at_tokens=trigger - _output_tokens(ModelPurpose.AGENT),
    )


async def _compaction_candidates(
    schedule: CompactSchedule,
    session: AsyncSession,
    conversation: Conversation,
    turns: list[CompletedTurn],
) -> tuple[list[CompletedTurn], int]:
    if len(turns) < 2:
        return [], 0
    limits = settings.agent_limits
    window = settings.effective_window(ModelPurpose.AGENT)
    output = _output_tokens(ModelPurpose.AGENT)
    fixed = await system_message(
        schedule.user_id,
        session,
        conversation,
        summary=None,
    )
    fixed_tokens = estimate_request_tokens(
        request_messages_for_model([fixed]), TOOL_DEFINITIONS
    )
    remaining = int(window * limits.compact_target_ratio) - fixed_tokens - output
    if remaining <= 0 and schedule.automatic:
        logger.info("对话 %s 固定上下文已超过压缩目标", conversation.conversation_id)
    summary_bytes = max(256, int(remaining * limits.compact_summary_share) - 96)
    if not schedule.automatic:
        return turns[:-1], summary_bytes
    history = [render_turn(turn.messages, current=False) for turn in turns]
    projected = await _projected_request_tokens(
        schedule.user_id, session, conversation, history
    )
    if projected + output <= window * limits.compact_trigger_ratio:
        return [], 0
    history_target = remaining - summary_bytes - 96
    kept_bytes = 0
    keep = 0
    for turn in reversed(history):
        cost = estimate_request_tokens(request_messages_for_model(turn), [])
        if keep and kept_bytes + cost > history_target:
            break
        keep += 1
        kept_bytes += cost
    return turns[:-keep], summary_bytes


async def schedule_compaction(schedule: CompactSchedule) -> CompactResult:
    async with schedule.sessions.begin() as session:
        scheduled = await _schedule(schedule, session)
    if isinstance(scheduled, CompactResult):
        return scheduled
    schedule.launch(
        scheduled.run_id, _run(schedule.sessions, schedule.client, scheduled)
    )
    return CompactResult(
        run_id=scheduled.run_id,
        status=RunStatus.RUNNING,
        focus_applied=bool(schedule.focus),
    )


async def _run(
    sessions: async_sessionmaker[AsyncSession],
    client: AsyncOpenAI,
    work: CompactWork,
) -> None:
    prompt = load_prompt("compact_conversation")
    try:
        response = await request_completion(
            sessions,
            work.user_id,
            work.run_id,
            client=client,
            purpose=ModelPurpose.COMPACT,
            prompt_version=prompt.label,
            messages=work.batch.messages,
            json_output=True,
            budget=CompactModelBudget(work.run_id),
            validate=_parse_summary,
            max_input_tokens=work.input_tokens,
            max_output_tokens=min(
                _output_tokens(ModelPurpose.COMPACT), work.summary_bytes
            ),
        )
        await _save_result(sessions, work, _parse_summary(response))
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.warning("压缩运行 %s 失败（%s）", work.run_id, describe_error(error))
        await _save_result(
            sessions,
            work,
            None,
            overflow=isinstance(error, ContextOverflowError),
        )


async def _save_result(
    sessions: async_sessionmaker[AsyncSession],
    work: CompactWork,
    summary: WorkSummary | None,
    *,
    overflow: bool = False,
) -> None:
    async with sessions.begin() as session:
        if not await lock_active_account(session, work.user_id):
            return
        conversation = await find_conversation(
            work.user_id, session, work.conversation_id, lock=True
        )
        if conversation is None:
            return
        run = await lock_compaction(
            work.user_id, session, work.conversation_id, work.run_id
        )
        if run is None:
            return
        updated = False
        if summary is not None:
            updated = await _write_summary(session, work, conversation, summary)
        if not updated:
            run.plan_json = {**(run.plan_json or {}), "overflow": overflow}
        await finish_run(
            session,
            work.user_id,
            work.run_id,
            RunStatus.SUCCEEDED if updated else RunStatus.FAILED,
        )


async def _write_summary(
    session: AsyncSession,
    work: CompactWork,
    conversation: Conversation,
    summary: WorkSummary,
) -> bool:
    previous = StoredWorkSummary.model_validate(conversation.summary_json or {})
    # 连续压缩保留已有且有证据的要求，重点说明不能自行挤掉它们。
    remembered_ids = {item.id for item in previous.requirements}
    requirements = [
        *previous.requirements,
        *(item for item in summary.requirements if item.id not in remembered_ids),
    ]
    carry_forward = previous.model_dump(
        include={"goal", "done_and_decided", "open_items", "unverified"}
        - summary.model_fields_set
    )
    summary = summary.model_copy(
        update={
            **carry_forward,
            "requirements": requirements,
            "answered_questions": [
                *previous.answered_questions,
                *summary.answered_questions,
            ],
        }
    )
    bodies, answers = await verified_sources(
        work.user_id,
        session,
        work.conversation_id,
        summary,
        work.answers,
        work.through_seq,
    )
    actions = await verified_actions(
        work.user_id, session, work.conversation_id, work.through_seq
    )
    checked = checked_summary(
        summary, bodies, answers, through_seq=work.through_seq, executed_actions=actions
    )
    fitted = fit_summary(checked, work.summary_bytes, work.through_seq)
    return await advance_summary(
        work.user_id,
        session,
        work.conversation_id,
        previous_revision=work.revision,
        previous_through_seq=work.previous_seq,
        through_seq=work.through_seq,
        summary_json=fitted.model_dump(mode="json"),
        unchanged_updated_at=conversation.updated_at,
    )
