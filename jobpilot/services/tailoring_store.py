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
    STOP_REASON_STATUS,
    AiRun,
    BulletFact,
    MatchReport,
    ResumeDocument,
    ResumeVersion,
    RunStatus,
    StopReason,
    TailoredBullet,
    TailoredResume,
)
from jobpilot.db.runs import finish_run
from jobpilot.services import drafts
from jobpilot.services.jobs import ParsedJob
from jobpilot.services.matching import FactCandidate, MatchItem
from jobpilot.services.resume_content import (
    BaseSection,
    BlockBullets,
    Bullet,
    assemble,
    bullet_of,
)
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
    base_sections: tuple[BaseSection, ...]  # 底稿里每段经历的位置，组装完整简历用
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
        base = (
            await session.execute(
                select(ResumeDocument.raw_text, ResumeDocument.sections_json).where(
                    ResumeDocument.doc_id == run.base_doc_id,
                    ResumeDocument.user_id == user_id,
                )
            )
        ).one()
        facts = await drafts.facts_in(session, user_id, run.fact_set_id)
    return TailorInputs(
        fact_set_id=run.fact_set_id,
        user_job_id=run.user_job_id,
        base_doc_id=run.base_doc_id,
        base_text=base.raw_text,
        base_sections=tuple(BaseSection.model_validate(s) for s in base.sections_json),
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


@dataclass(frozen=True)
class _Rows:
    bullets: list[TailoredBullet]
    refs: list[BulletFact]
    blocks: list[BlockBullets]  # 组装完整简历用，按计划顺序


def _rows(
    user_id: UUID,
    run_id: UUID,
    fact_set_id: UUID,
    plan: Sequence[PlanBlock],
    outcomes: Mapping[str, BlockOutcome],
) -> _Rows:
    """按计划顺序编号的条目行、引用行，以及组装用的每块条目。"""
    rows = _Rows(bullets=[], refs=[], blocks=[])
    for block in plan:
        outcome = outcomes.get(block.key)
        content: list[Bullet] = []
        for bullet in outcome.bullets if outcome else ():
            bullet_id = uuid4()
            rows.bullets.append(
                TailoredBullet(
                    bullet_id=bullet_id,
                    run_id=run_id,
                    user_id=user_id,
                    fact_set_id=fact_set_id,
                    section=block.bullet_section.value,
                    experience=block.experience,
                    order_no=len(rows.bullets),
                    text=bullet.text,
                    check_status=bullet.check_status.value,
                    revision_count=bullet.revision_count,
                    issues_json=[
                        issue.model_dump(mode="json") for issue in bullet.issues
                    ],
                )
            )
            rows.refs.extend(
                BulletFact(
                    bullet_id=bullet_id,
                    fact_id=fact_id,
                    user_id=user_id,
                    fact_set_id=fact_set_id,
                )
                for fact_id in bullet.fact_ids
            )
            content.append(
                bullet_of(
                    bullet_id,
                    bullet.text,
                    bullet.fact_ids,
                    bullet.check_status,
                    bullet.issues,
                )
            )
        rows.blocks.append(
            BlockBullets(
                experience=block.experience, section=block.section, bullets=content
            )
        )
    return rows


def _first_version(
    user_id: UUID, run_id: UUID, inputs: TailorInputs, blocks: list[BlockBullets]
) -> tuple[TailoredResume, ResumeVersion]:
    """组装完整简历：简历和第一个版本，当前版本指针指向它。"""
    content = assemble(inputs.base_text, inputs.base_sections, blocks)
    resume_id, version_id = uuid4(), uuid4()
    return (
        TailoredResume(
            resume_id=resume_id,
            run_id=run_id,
            user_id=user_id,
            current_version_id=version_id,
        ),
        ResumeVersion(
            resume_version_id=version_id,
            resume_id=resume_id,
            user_id=user_id,
            version=1,
            content_json=content.model_dump(mode="json"),
        ),
    )


async def save_outcome(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: UUID,
    run_id: UUID,
    *,
    inputs: TailorInputs,
    plan: Sequence[PlanBlock],
    outcomes: Sequence[BlockOutcome],
    stop_reason: StopReason | None,
) -> RunStatus:
    """一个事务写入条目、引用、计划和终态；运行已经结束时整体回滚。

    条目按计划顺序编号；stop_reason 为空时按各块的结果判断。
    成功时同一事务组装完整简历和第一个版本（06 T2.5：完整简历组装成功后才发布）；
    预算用完或失败时不组装。抛出 RunAlreadyFinishedError 时什么都没写入。
    """
    by_key = {outcome.key: outcome for outcome in outcomes}
    stop_reason = stop_reason or stop_reason_of(outcomes)
    status = STOP_REASON_STATUS[stop_reason] if stop_reason else RunStatus.SUCCEEDED

    rows = _rows(user_id, run_id, inputs.fact_set_id, plan, by_key)
    async with session_factory.begin() as session:
        # 先在同一事务里取得终态：运行已被别人结束（或 finalize 重复执行）时在这里停下，
        # 什么都还没写。
        await finish_run(session, user_id, run_id, status)
        session.add_all(rows.bullets)
        await session.flush()  # 两张表之间没有 ORM 关系，先写条目，引用才有外键可指
        session.add_all(rows.refs)
        if status is RunStatus.SUCCEEDED:
            session.add_all(_first_version(user_id, run_id, inputs, rows.blocks))
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
