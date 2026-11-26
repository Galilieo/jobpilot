"""改写一块（一段经历或技能概述）：改写 → 依据检查 → 修订或回退。

每块一次改写调用，写出的每条各自检查、修订（02 第 3 节）：
- 第一、二层不过且还能修订时先修订，省一次第三层调用（should_run_semantic）；
- 最多修订 MAX_REVISIONS 次，仍不过就回退成引用事实的原文；
- 修订调用本身出错（不是预算用完）也回退：原文照抄，不必再检查。
预算用完时这块停下：已经定稿的条目保留，没定稿的不保留（没检查过的文字不能给用户看），
这块标为中断。查询结果只来自业务表，见 06 T2.4。
"""

import asyncio
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from openai import APIError, AsyncOpenAI
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.db.models import CheckStatus, FactSource
from jobpilot.services.facts import TruncatedOutputError
from jobpilot.services.grounding import (
    GroundingFact,
    GroundingIssue,
    check_grounding,
    should_run_semantic,
)
from jobpilot.services.llm_gateway import (
    BudgetExhaustedError,
    EmptyModelResponseError,
    ModelBudget,
)
from jobpilot.services.rewriting import (
    DraftBullet,
    RewriteTask,
    revise_bullet,
    rewrite_section,
)
from jobpilot.services.semantic_check import ground_check
from jobpilot.services.tailoring_plan import PlanBlock

# 与 tailored_bullet 的约束 revision_count BETWEEN 0 AND 2 一致。
MAX_REVISIONS = 2
FALLBACK_SEPARATOR = "；"
# 模型调用失败（不含预算用完）：重试和一次 JSON 修复之后仍然不行。
MODEL_ERRORS = (
    APIError,
    ValidationError,
    TruncatedOutputError,
    EmptyModelResponseError,
)


class CheckLevel(StrEnum):
    """评测消融用的依据检查级别，产品始终使用 FULL。"""

    NONE = "none"
    PROGRAM = "program"
    FULL = "full"


class BlockStatus(StrEnum):
    DONE = "done"
    INTERRUPTED = "interrupted"  # 预算用完
    FAILED = "failed"  # 改写调用失败


class FinalBullet(BaseModel):
    """一条改写结果；仅评测跳过检查时 check_status 为空。"""

    text: str
    fact_ids: tuple[UUID, ...]
    check_status: CheckStatus | None
    revision_count: int
    issues: tuple[GroundingIssue, ...] = ()  # 最后一次检查的问题；通过时为空


class BlockOutcome(BaseModel):
    key: str
    status: BlockStatus
    bullets: tuple[FinalBullet, ...] = ()


@dataclass(frozen=True)
class WriteContext:
    """一次改写运行里各块共用的输入。"""

    session_factory: async_sessionmaker[AsyncSession]
    user_id: UUID
    run_id: UUID
    client: AsyncOpenAI
    budget: ModelBudget
    limiter: asyncio.Semaphore  # 同时进行的模型请求上限
    available: Mapping[UUID, GroundingFact]  # 本次运行固定的事实库
    known_organizations: frozenset[str]
    job_title: str | None
    keywords: tuple[str, ...]
    check_level: CheckLevel = CheckLevel.FULL


async def write_block(ctx: WriteContext, block: PlanBlock) -> BlockOutcome:
    """改写一块并逐条定稿。只有预算用完和改写调用失败会让这块不完整。"""
    facts = [ctx.available[fact_id] for fact_id in block.fact_ids]
    task = RewriteTask(
        section=block.bullet_section,
        experience=block.experience,
        facts=facts,
        focus_requirements=[focus.text for focus in block.focus],
        job_title=ctx.job_title,
        keywords=ctx.keywords,
        max_bullets=block.max_bullets,
    )
    try:
        async with ctx.limiter:
            drafts = await rewrite_section(
                ctx.session_factory,
                ctx.user_id,
                ctx.run_id,
                client=ctx.client,
                budget=ctx.budget,
                task=task,
            )
    except BudgetExhaustedError:
        return BlockOutcome(key=block.key, status=BlockStatus.INTERRUPTED)
    except MODEL_ERRORS:
        return BlockOutcome(key=block.key, status=BlockStatus.FAILED)

    if ctx.check_level is CheckLevel.NONE:
        return BlockOutcome(
            key=block.key,
            status=BlockStatus.DONE,
            bullets=tuple(
                FinalBullet(
                    text=draft.text,
                    fact_ids=draft.fact_ids,
                    check_status=None,
                    revision_count=0,
                )
                for draft in drafts
            ),
        )

    results = await asyncio.gather(
        *(_settle(ctx, block, facts, draft) for draft in drafts),
        return_exceptions=True,
    )
    return _collect(block.key, results)


def _collect(key: str, results: Iterable[FinalBullet | BaseException]) -> BlockOutcome:
    """按原顺序收起各条结果。预算用完的条目不保留；其他异常是程序错误，照常抛出。"""
    bullets, interrupted = [], False
    for result in results:
        if isinstance(result, BudgetExhaustedError):
            interrupted = True
        elif isinstance(result, BaseException):
            raise result
        else:
            bullets.append(result)
    status = BlockStatus.INTERRUPTED if interrupted else BlockStatus.DONE
    return BlockOutcome(key=key, status=status, bullets=tuple(bullets))


async def _settle(
    ctx: WriteContext,
    block: PlanBlock,
    facts: Sequence[GroundingFact],
    draft: DraftBullet,
) -> FinalBullet:
    """检查一条，不过就修订，最多 MAX_REVISIONS 次，仍不过就回退。"""
    revisions = 0
    while True:
        issues = await _check(ctx, block, draft, MAX_REVISIONS - revisions)
        if not issues:
            return FinalBullet(
                text=draft.text,
                fact_ids=draft.fact_ids,
                check_status=CheckStatus.PASSED,
                revision_count=revisions,
            )
        if revisions == MAX_REVISIONS:
            return fallback(block, ctx.available, draft, issues, revisions)
        try:
            async with ctx.limiter:
                draft = await revise_bullet(
                    ctx.session_factory,
                    ctx.user_id,
                    ctx.run_id,
                    client=ctx.client,
                    budget=ctx.budget,
                    bullet=draft,
                    facts=facts,
                    issues=issues,
                )
        except MODEL_ERRORS:
            return fallback(block, ctx.available, draft, issues, revisions)
        revisions += 1


async def _check(
    ctx: WriteContext, block: PlanBlock, draft: DraftBullet, revisions_left: int
) -> tuple[GroundingIssue, ...]:
    """执行前两层检查，完整模式再按规则决定是否调第三层。"""
    issues = check_grounding(
        draft.text,
        draft.fact_ids,
        ctx.available,
        section=block.bullet_section,
        experience=block.experience,
        known_organizations=ctx.known_organizations,
    ).issues
    if ctx.check_level is CheckLevel.PROGRAM or not should_run_semantic(
        issues, revisions_left=revisions_left
    ):
        return issues
    async with ctx.limiter:
        semantic = await ground_check(
            ctx.session_factory,
            ctx.user_id,
            ctx.run_id,
            client=ctx.client,
            budget=ctx.budget,
            sentence=draft.text,
            facts=[ctx.available[fact_id] for fact_id in draft.fact_ids],
        )
    return issues + semantic.issues


def fallback_from_facts(
    fact_ids: Sequence[UUID],
    available: Mapping[UUID, GroundingFact],
    *,
    experience: str | None,
) -> tuple[str, tuple[UUID, ...]] | None:
    """用本次事实库里仍有效的引用事实拼出回退原文，无有效事实时返回 None。"""
    valid = [
        available[fact_id]
        for fact_id in fact_ids
        if fact_id in available and _usable_fact(available[fact_id], experience)
    ]
    if not valid:
        return None
    return (
        FALLBACK_SEPARATOR.join(fact.text for fact in valid),
        tuple(fact.fact_id for fact in valid),
    )


def fallback(
    block: PlanBlock,
    available: Mapping[UUID, GroundingFact],
    draft: DraftBullet,
    issues: tuple[GroundingIssue, ...],
    revisions: int,
) -> FinalBullet:
    """回退成引用事实的原文，按顺序用「；」接成一条。

    只用能通过第一层的事实：在本次事实库里、来自简历的通过了原文校验、
    经历条目只用同一段经历的。一条都没有时标「待处理」，保留模型的文字给用户看。
    问题清单保留最后一次检查的结果，说明为什么没有用模型的写法。
    """
    assembled = fallback_from_facts(
        draft.fact_ids, available, experience=block.experience
    )
    if assembled is None:
        return FinalBullet(
            text=draft.text,
            fact_ids=draft.fact_ids,
            check_status=CheckStatus.PENDING,
            revision_count=revisions,
            issues=issues,
        )
    text, valid_ids = assembled
    return FinalBullet(
        text=text,
        fact_ids=valid_ids,
        check_status=CheckStatus.FALLBACK,
        revision_count=revisions,
        issues=issues,
    )


def _usable_fact(fact: GroundingFact, experience: str | None) -> bool:
    if fact.source is FactSource.RESUME and not fact.quote_verified:
        return False
    return experience is None or fact.experience == experience
