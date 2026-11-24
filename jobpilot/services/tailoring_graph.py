"""改写流程图（02 第 3 节）：load_inputs → match → plan → 各块并行改写 → finalize。

- match：事实库版本和岗位快照都没变时沿用最近的匹配报告，否则新建一次匹配运行。
- 各块用 Send 分发、并行执行（LangGraph map-reduce），共用同一份预算和请求上限。
- 预算用完或调用失败不抛到图外：记在状态里，由 finalize 写进业务表并给出终态。
  查询结果只读业务表，不读检查点；V1 不从检查点续跑（02 第 3.3 节）。
"""

import asyncio
import operator
from dataclasses import dataclass
from typing import Annotated, Any, TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Send
from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.db.models import AiRun, RunStatus, SectionKind, StopReason
from jobpilot.db.reports import add_match_report
from jobpilot.db.runs import finish_run
from jobpilot.services.grounding import organizations_of
from jobpilot.services.llm_gateway import BudgetExhaustedError, ModelBudget
from jobpilot.services.matching import MatchItem, MatchValidationError, match_job
from jobpilot.services.tailoring import (
    MODEL_ERRORS,
    BlockOutcome,
    WriteContext,
    write_block,
)
from jobpilot.services.tailoring_plan import PlanBlock, build_plan
from jobpilot.services.tailoring_store import (
    TailorInputs,
    load_inputs,
    reusable_match_items,
    save_outcome,
)


@dataclass(frozen=True)
class TailorDeps:
    """不放进图状态的依赖：数据库、模型客户端、预算和并发上限。"""

    session_factory: async_sessionmaker[AsyncSession]
    client: AsyncOpenAI
    budget: ModelBudget
    limiter: asyncio.Semaphore


class TailorState(TypedDict, total=False):
    user_id: UUID
    run_id: UUID
    section_overrides: dict[str, SectionKind]
    inputs: TailorInputs
    match_items: list[MatchItem]
    stop_reason: StopReason | None
    plan: list[PlanBlock]
    outcomes: Annotated[list[BlockOutcome], operator.add]  # 各块并行写入，合并


class BlockTask(TypedDict):
    """Send 给 write_block 的一块。"""

    user_id: UUID
    run_id: UUID
    inputs: TailorInputs
    block: PlanBlock


def build_tailor_graph(
    deps: TailorDeps, checkpointer: Any = None
) -> CompiledStateGraph:
    async def load(state: TailorState) -> dict[str, Any]:
        inputs = await load_inputs(
            deps.session_factory, state["user_id"], state["run_id"]
        )
        return {"inputs": inputs}

    async def match(state: TailorState) -> dict[str, Any]:
        inputs = state["inputs"]
        reused = await reusable_match_items(
            deps.session_factory, state["user_id"], inputs
        )
        if reused is not None:
            return {"match_items": reused}
        return await run_match(deps, state["user_id"], inputs)

    def plan(state: TailorState) -> dict[str, Any]:
        inputs = state["inputs"]
        blocks = build_plan(
            inputs.facts,
            inputs.snapshot.requirements,
            state["match_items"],
            base_doc_id=inputs.base_doc_id,
            base_text=inputs.base_text,
            section_overrides=state.get("section_overrides"),
        )
        return {"plan": blocks}

    def after_match(state: TailorState) -> str:
        return "finalize" if state.get("stop_reason") else "plan"

    def dispatch(state: TailorState) -> list[Send] | str:
        blocks = state["plan"]
        if not blocks:
            return "finalize"
        return [
            Send(
                "write_block",
                BlockTask(
                    user_id=state["user_id"],
                    run_id=state["run_id"],
                    inputs=state["inputs"],
                    block=block,
                ),
            )
            for block in blocks
        ]

    async def write(task: BlockTask) -> dict[str, Any]:
        outcome = await write_block(_context(deps, task), task["block"])
        return {"outcomes": [outcome]}

    async def finalize(state: TailorState) -> dict[str, Any]:
        await save_outcome(
            deps.session_factory,
            state["user_id"],
            state["run_id"],
            inputs=state["inputs"],
            plan=state.get("plan", []),
            outcomes=state.get("outcomes", []),
            stop_reason=state.get("stop_reason"),
        )
        return {}

    graph = StateGraph(TailorState)
    graph.add_node("load_inputs", load)
    graph.add_node("match", match)
    graph.add_node("plan", plan)
    graph.add_node("write_block", write)
    graph.add_node("finalize", finalize)
    graph.add_edge(START, "load_inputs")
    graph.add_edge("load_inputs", "match")
    graph.add_conditional_edges("match", after_match, ["plan", "finalize"])
    graph.add_conditional_edges("plan", dispatch, ["write_block", "finalize"])
    graph.add_edge("write_block", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer)


def _context(deps: TailorDeps, task: BlockTask) -> WriteContext:
    inputs = task["inputs"]
    available = {fact.fact_id: fact.grounding for fact in inputs.facts}
    return WriteContext(
        session_factory=deps.session_factory,
        user_id=task["user_id"],
        run_id=task["run_id"],
        client=deps.client,
        budget=deps.budget,
        limiter=deps.limiter,
        available=available,
        known_organizations=frozenset(organizations_of(available.values())),
        job_title=inputs.snapshot.basic_fields.title,
        keywords=tuple(inputs.snapshot.keywords),
    )


async def run_match(
    deps: TailorDeps, user_id: UUID, inputs: TailorInputs
) -> dict[str, Any]:
    """新建一次匹配运行，用改写运行的预算调用模型，报告和终态在同一个事务提交。

    返回图状态的更新：成功时给 match_items，否则给 stop_reason。
    与匹配接口（api/matching.py 的 create_match_report）共用调用、建报告和收尾的函数，
    只有编排不同，所以不合并：接口要在请求的 session 里建运行（幂等键在 flush 时绑定）、
    把重放快照和报告放在同一事务、出错返回 502；这里用独立短事务、共用改写的预算、
    出错记为停止原因。
    """
    snapshot_json = inputs.snapshot.model_dump(mode="json")
    async with deps.session_factory.begin() as session:
        run = AiRun(
            user_id=user_id,
            kind="match",
            user_job_id=inputs.user_job_id,
            fact_set_id=inputs.fact_set_id,
            job_snapshot_json=snapshot_json,
        )
        session.add(run)
        await session.flush()
        match_run_id = run.run_id
    try:
        built = await match_job(
            deps.session_factory,
            user_id,
            match_run_id,
            fact_set_id=inputs.fact_set_id,
            client=deps.client,
            budget=deps.budget,
            snapshot=inputs.snapshot,
            facts=inputs.candidates,
        )
    except BudgetExhaustedError:
        await _finish(deps, user_id, match_run_id, RunStatus.BUDGET_EXHAUSTED)
        return {"stop_reason": StopReason.BUDGET_EXHAUSTED}
    except (*MODEL_ERRORS, MatchValidationError):
        await _finish(deps, user_id, match_run_id, RunStatus.FAILED)
        return {"stop_reason": StopReason.MATCH_FAILED}

    async with deps.session_factory.begin() as session:
        match_run = await session.get_one(AiRun, match_run_id)
        add_match_report(
            session,
            match_run,
            items=[item.model_dump(mode="json") for item in built.items],
            main_gaps=built.main_gaps,
        )
        await finish_run(session, user_id, match_run_id, RunStatus.SUCCEEDED)
    return {"match_items": built.items}


async def _finish(
    deps: TailorDeps, user_id: UUID, run_id: UUID, status: RunStatus
) -> None:
    async with deps.session_factory.begin() as session:
        await finish_run(session, user_id, run_id, status)


async def run_tailoring(
    deps: TailorDeps,
    user_id: UUID,
    run_id: UUID,
    *,
    section_overrides: dict[str, SectionKind] | None = None,
) -> None:
    """执行一次已创建的改写运行，直到写入终态。调用前需已提交 ai_run。"""
    graph = build_tailor_graph(deps)
    await graph.ainvoke(
        TailorState(
            user_id=user_id,
            run_id=run_id,
            section_overrides=section_overrides or {},
            stop_reason=None,
            outcomes=[],
        )
    )
