"""改写运行的数据库读写：读取运行固定的输入、找可以沿用的匹配报告、保存结果。

查询改写结果只读这里写入的业务表（tailored_bullet、bullet_fact、ai_run.plan_json），
不读 LangGraph 检查点（评审 2026-11-24）。
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.db.models import (
    AiRun,
    BulletFact,
    Fact,
    MatchReport,
    ResumeDocument,
    RunStatus,
    StopReason,
    TailoredBullet,
)
from jobpilot.db.runs import finish_run
from jobpilot.services.jobs import ParsedJob
from jobpilot.services.matching import FactCandidate, MatchItem
from jobpilot.services.tailoring import BlockOutcome, BlockStatus
from jobpilot.services.tailoring_plan import PlanBlock, TailorFact

NOT_STARTED = "not_started"


class TailorInputError(LookupError):
    """运行不存在、不属于该用户，或缺少启动时应固定的输入。"""


@dataclass(frozen=True)
class TailorInputs:
    """改写运行启动时固定的输入。"""

    fact_set_id: UUID
    user_job_id: UUID
    base_doc_id: UUID
    base_text: str
    snapshot: ParsedJob
    facts: tuple[TailorFact, ...]
    candidates: tuple[FactCandidate, ...]  # 同一批事实，给匹配用


async def load_inputs(
    session_factory: async_sessionmaker[AsyncSession], user_id: UUID, run_id: UUID
) -> TailorInputs:
    async with session_factory() as session:
        run = await session.scalar(
            select(AiRun).where(
                AiRun.run_id == run_id, AiRun.user_id == user_id, AiRun.kind == "tailor"
            )
        )
        if (
            run is None
            or run.fact_set_id is None
            or run.user_job_id is None
            or run.base_doc_id is None
            or run.job_snapshot_json is None
        ):
            raise TailorInputError(run_id)
        base_text = await session.scalar(
            select(ResumeDocument.raw_text).where(
                ResumeDocument.doc_id == run.base_doc_id,
                ResumeDocument.user_id == user_id,
            )
        )
        facts = (
            await session.scalars(
                select(Fact)
                .where(Fact.fact_set_id == run.fact_set_id, Fact.user_id == user_id)
                # 同一版本的事实在一个事务里写入，created_at 相同；
                # 再按出处和原文位置排，才是简历本来的顺序（用户补充的排后面）。
                .order_by(
                    Fact.created_at,
                    Fact.source_doc_id.nulls_last(),
                    Fact.span_start.nulls_last(),
                    Fact.fact_id,
                )
            )
        ).all()
    return TailorInputs(
        fact_set_id=run.fact_set_id,
        user_job_id=run.user_job_id,
        base_doc_id=run.base_doc_id,
        base_text=base_text or "",
        snapshot=ParsedJob.model_validate(run.job_snapshot_json),
        facts=tuple(TailorFact.of(fact) for fact in facts),
        candidates=tuple(FactCandidate.from_fact(fact) for fact in facts),
    )


async def reusable_match_items(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: UUID,
    inputs: TailorInputs,
) -> list[MatchItem] | None:
    """事实库版本和岗位快照都没变时，沿用最近一份成功的匹配报告。"""
    async with session_factory() as session:
        items = await session.scalar(
            select(MatchReport.items_json)
            .join(
                AiRun,
                (AiRun.run_id == MatchReport.run_id)
                & (AiRun.user_id == MatchReport.user_id),
            )
            .where(
                AiRun.user_id == user_id,
                AiRun.kind == "match",
                AiRun.status == RunStatus.SUCCEEDED,
                AiRun.user_job_id == inputs.user_job_id,
                AiRun.fact_set_id == inputs.fact_set_id,
                AiRun.job_snapshot_json == inputs.snapshot.model_dump(mode="json"),
            )
            .order_by(AiRun.finished_at.desc())
            .limit(1)
        )
    if items is None:
        return None
    return [MatchItem.model_validate(item) for item in items]


_STATUS_OF = {
    StopReason.BUDGET_EXHAUSTED: RunStatus.BUDGET_EXHAUSTED,
    StopReason.MATCH_FAILED: RunStatus.FAILED,
    StopReason.REWRITE_FAILED: RunStatus.FAILED,
}


def stop_reason_of(outcomes: Sequence[BlockOutcome]) -> StopReason | None:
    """有一块因预算中断就算预算用完；否则有一块失败就算改写失败。"""
    statuses = {outcome.status for outcome in outcomes}
    if BlockStatus.INTERRUPTED in statuses:
        return StopReason.BUDGET_EXHAUSTED
    if BlockStatus.FAILED in statuses:
        return StopReason.REWRITE_FAILED
    return None


def plan_record(
    plan: Sequence[PlanBlock], outcomes: Mapping[str, BlockOutcome]
) -> dict[str, Any]:
    """写进 ai_run.plan_json：每块的计划和结果，没轮到的标 not_started。"""
    return {
        "blocks": [
            {
                **block.model_dump(mode="json", exclude={"fact_ids"}),
                "status": outcomes[block.key].status.value
                if block.key in outcomes
                else NOT_STARTED,
            }
            for block in plan
        ]
    }


def _rows(
    user_id: UUID,
    run_id: UUID,
    fact_set_id: UUID,
    plan: Sequence[PlanBlock],
    outcomes: Mapping[str, BlockOutcome],
) -> tuple[list[TailoredBullet], list[BulletFact]]:
    """按计划顺序编号的条目行和引用行。"""
    bullets, refs = [], []
    for block in plan:
        outcome = outcomes.get(block.key)
        for bullet in outcome.bullets if outcome else ():
            bullet_id = uuid4()
            bullets.append(
                TailoredBullet(
                    bullet_id=bullet_id,
                    run_id=run_id,
                    user_id=user_id,
                    fact_set_id=fact_set_id,
                    section=block.bullet_section.value,
                    experience=block.experience,
                    order_no=len(bullets),
                    text=bullet.text,
                    check_status=bullet.check_status.value,
                    revision_count=bullet.revision_count,
                    issues_json=[
                        issue.model_dump(mode="json") for issue in bullet.issues
                    ],
                )
            )
            refs.extend(
                BulletFact(
                    bullet_id=bullet_id,
                    fact_id=fact_id,
                    user_id=user_id,
                    fact_set_id=fact_set_id,
                )
                for fact_id in bullet.fact_ids
            )
    return bullets, refs


async def save_outcome(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: UUID,
    run_id: UUID,
    *,
    fact_set_id: UUID,
    plan: Sequence[PlanBlock],
    outcomes: Sequence[BlockOutcome],
    stop_reason: StopReason | None,
) -> RunStatus:
    """一个事务写入条目、引用、计划和终态；运行已经结束时整体回滚。

    条目按计划顺序编号；stop_reason 为空时按各块的结果判断。
    抛出 RunAlreadyFinishedError 时什么都没写入。
    """
    by_key = {outcome.key: outcome for outcome in outcomes}
    stop_reason = stop_reason or stop_reason_of(outcomes)
    status = _STATUS_OF[stop_reason] if stop_reason else RunStatus.SUCCEEDED

    bullets, refs = _rows(user_id, run_id, fact_set_id, plan, by_key)
    async with session_factory.begin() as session:
        session.add_all(bullets)
        await session.flush()  # 两张表之间没有 ORM 关系，先写条目，引用才有外键可指
        session.add_all(refs)
        # 运行已被别人结束时这里抛错，上面的写入随事务一起回滚。
        await finish_run(session, user_id, run_id, status)
        # 停止原因只能写在已结束的运行上（ck_ai_run_stop_reason），所以放在收尾之后。
        await session.execute(
            update(AiRun)
            .where(AiRun.run_id == run_id, AiRun.user_id == user_id)
            .values(
                plan_json=plan_record(plan, by_key),
                stop_reason=stop_reason.value if stop_reason else None,
            )
        )
    return status
