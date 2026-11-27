"""从只读快照汇总单个账号的使用统计数字，供运维核对与工作台复用。

本模块只读取聚合计数和版本差异，不修改数据库、不调用模型，也不返回简历、JD 或对话原文。
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from jobpilot.db.application_schema import ApplicationStage
from jobpilot.db.models import (
    AiRun,
    Application,
    CallOutcome,
    CheckStatus,
    Conversation,
    Message,
    MessageRole,
    ModelCall,
    ResumeVersion,
    TailoredBullet,
    TailoredResume,
    User,
    UserDecision,
    normalize_email,
)
from jobpilot.db.session import read_snapshot
from jobpilot.services.resume_content import (
    Bullet,
    ResumeContent,
    UserBulletChoice,
    plain,
)


@dataclass(frozen=True)
class GroundingCounts:
    total: int
    passed: int
    fallback: int
    pending: int
    resume_count: int
    rejected: int


@dataclass(frozen=True)
class ConversationStats:
    total_conversations: int
    completed_jd_to_resume_flows: int
    dialogue_tailor_runs: int


@dataclass(frozen=True)
class BulletEditCounts:
    resumes_with_edits: int
    edited_bullets: int
    use_facts_bullets: int
    removed_bullets: int


@dataclass(frozen=True)
class ApplicationStats:
    total: int
    by_stage: dict[str, int]


@dataclass(frozen=True)
class ModelUsageStats:
    total_calls: int
    succeeded_calls: int
    unknown_usage_calls: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class AccountUsageStats:
    conversations: ConversationStats
    grounding: GroundingCounts
    edits: BulletEditCounts
    applications: ApplicationStats
    model_usage: ModelUsageStats


async def grounding_counts(session: AsyncSession, user_id: UUID) -> GroundingCounts:
    """统计指定账号全部定制条目的依据检查结果、拒绝改写数及定制简历份数。"""
    rows = (
        await session.execute(
            select(
                TailoredBullet.check_status,
                TailoredBullet.user_decision,
                func.count(),
            )
            .where(TailoredBullet.user_id == user_id)
            .group_by(TailoredBullet.check_status, TailoredBullet.user_decision)
        )
    ).all()
    by_status: dict[str, int] = {}
    rejected = 0
    for status, decision, count in rows:
        by_status[status] = by_status.get(status, 0) + count
        if decision == UserDecision.REJECTED:
            rejected += count
    passed = by_status.get(CheckStatus.PASSED, 0)
    fallback = by_status.get(CheckStatus.FALLBACK, 0)
    pending = by_status.get(CheckStatus.PENDING, 0)
    resume_count = (
        await session.scalar(
            select(func.count(TailoredResume.resume_id)).where(
                TailoredResume.user_id == user_id
            )
        )
        or 0
    )
    return GroundingCounts(
        total=passed + fallback + pending,
        passed=passed,
        fallback=fallback,
        pending=pending,
        resume_count=resume_count,
        rejected=rejected,
    )


def _parsed_uuid(raw: object) -> UUID | None:
    if not isinstance(raw, str):
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None


async def _tailor_run_jobs(
    session: AsyncSession, user_id: UUID, run_ids: set[UUID]
) -> dict[UUID, UUID | None]:
    if not run_ids:
        return {}
    rows = (
        await session.execute(
            select(AiRun.run_id, AiRun.user_job_id).where(
                AiRun.user_id == user_id,
                AiRun.kind == "tailor",
                AiRun.run_id.in_(run_ids),
            )
        )
    ).all()
    return {run_id: job_id for run_id, job_id in rows}


async def _conversation_stats(
    session: AsyncSession, user_id: UUID
) -> ConversationStats:
    total = (
        await session.scalar(
            select(func.count(Conversation.conversation_id)).where(
                Conversation.user_id == user_id
            )
        )
        or 0
    )
    rows = (
        await session.execute(
            select(Message.conversation_id, Message.steps_json)
            .where(Message.user_id == user_id, Message.role == MessageRole.TOOL)
            .order_by(Message.conversation_id, Message.seq)
        )
    ).all()
    tailor_runs = {
        run_id
        for _, steps in rows
        for step in steps
        if isinstance(step, dict)
        and step.get("tool") == "tailor_resume"
        and step.get("status") == "succeeded"
        and (run_id := _parsed_uuid(step.get("run_id"))) is not None
    }
    run_jobs = await _tailor_run_jobs(session, user_id, tailor_runs)
    completed = _count_completed_flows(rows, run_jobs)
    return ConversationStats(
        total_conversations=total,
        completed_jd_to_resume_flows=completed,
        dialogue_tailor_runs=len(tailor_runs),
    )


def _count_completed_flows(rows: list[Any], run_jobs: dict[UUID, UUID | None]) -> int:
    parsed_jobs_by_conv: dict[UUID, set[UUID]] = {}
    has_parsed_jd: set[UUID] = set()
    completed_keys: set[tuple[UUID, UUID | None]] = set()
    for conv_id, steps in rows:
        for step in steps:
            if not isinstance(step, dict) or step.get("status") != "succeeded":
                continue
            tool = step.get("tool")
            if tool == "parse_jd":
                has_parsed_jd.add(conv_id)
                if (job_id := _parsed_uuid(step.get("job_id"))) is not None:
                    parsed_jobs_by_conv.setdefault(conv_id, set()).add(job_id)
            elif tool == "tailor_resume" and conv_id in has_parsed_jd:
                run_id = _parsed_uuid(step.get("run_id"))
                job_id = run_jobs.get(run_id) if run_id is not None else None
                if job_id is not None and job_id in parsed_jobs_by_conv.get(
                    conv_id, ()
                ):
                    completed_keys.add((conv_id, job_id))
                elif run_id is None or run_id not in run_jobs:
                    completed_keys.add((conv_id, None))
    return len(completed_keys)


def _bullets_by_id(content: ResumeContent) -> dict[UUID, Bullet]:
    return {
        bullet.bullet_id: bullet
        for section in content.sections
        for entry in section.entries
        for bullet in entry.bullets
    }


def _diff_resume_versions(
    v1_json: dict[str, Any], cur_json: dict[str, Any]
) -> tuple[int, int, int]:
    v1_bullets = _bullets_by_id(ResumeContent.from_stored(v1_json))
    cur_bullets = _bullets_by_id(ResumeContent.from_stored(cur_json))
    edited = 0
    use_facts = 0
    removed = 0
    for bullet_id, b1 in v1_bullets.items():
        b_cur = cur_bullets.get(bullet_id)
        if b_cur is None:
            removed += 1
        elif (
            b_cur.user_choice is UserBulletChoice.USE_FACTS
            and b1.user_choice is not UserBulletChoice.USE_FACTS
        ):
            use_facts += 1
        elif b_cur.user_choice is not UserBulletChoice.USE_FACTS and plain(
            b_cur.text
        ) != plain(b1.text):
            edited += 1
    return edited, use_facts, removed


async def _bullet_edit_counts(session: AsyncSession, user_id: UUID) -> BulletEditCounts:
    v1 = aliased(ResumeVersion)
    cur = aliased(ResumeVersion)
    rows = (
        await session.execute(
            select(
                v1.resume_version_id,
                cur.resume_version_id,
                v1.content_json,
                cur.content_json,
            )
            .select_from(TailoredResume)
            .join(
                v1,
                (v1.resume_id == TailoredResume.resume_id)
                & (v1.user_id == TailoredResume.user_id)
                & (v1.version == 1),
            )
            .join(
                cur,
                (cur.resume_version_id == TailoredResume.current_version_id)
                & (cur.user_id == TailoredResume.user_id),
            )
            .where(TailoredResume.user_id == user_id)
        )
    ).all()
    resumes_with_edits = edited = use_facts = removed = 0
    for v1_id, cur_id, v1_json, cur_json in rows:
        if v1_id == cur_id:
            continue
        e, u, r = _diff_resume_versions(v1_json, cur_json)
        if e or u or r:
            resumes_with_edits += 1
            edited += e
            use_facts += u
            removed += r
    return BulletEditCounts(
        resumes_with_edits=resumes_with_edits,
        edited_bullets=edited,
        use_facts_bullets=use_facts,
        removed_bullets=removed,
    )


async def _application_stats(session: AsyncSession, user_id: UUID) -> ApplicationStats:
    rows = (
        await session.execute(
            select(Application.stage, func.count())
            .where(Application.user_id == user_id)
            .group_by(Application.stage)
        )
    ).all()
    by_stage = {stage.value: 0 for stage in ApplicationStage}
    for stage, count in rows:
        by_stage[stage] = count
    return ApplicationStats(total=sum(by_stage.values()), by_stage=by_stage)


async def _model_usage_stats(session: AsyncSession, user_id: UUID) -> ModelUsageStats:
    row = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(ModelCall.outcome == CallOutcome.SUCCESS),
                func.count().filter(
                    ModelCall.prompt_tokens.is_(None)
                    | ModelCall.completion_tokens.is_(None)
                ),
                func.coalesce(func.sum(ModelCall.prompt_tokens), 0),
                func.coalesce(func.sum(ModelCall.completion_tokens), 0),
            ).where(ModelCall.user_id == user_id)
        )
    ).one()
    total_calls, succeeded, unknown_usage, prompt_tokens, completion_tokens = row
    return ModelUsageStats(
        total_calls=total_calls,
        succeeded_calls=succeeded,
        unknown_usage_calls=unknown_usage,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )


async def _resolve_user_id(
    session: AsyncSession, *, email: str | None, user_id: UUID | None
) -> UUID | None:
    if user_id is not None:
        return await session.scalar(select(User.user_id).where(User.user_id == user_id))
    if email is not None:
        normalized = normalize_email(email)
        return await session.scalar(
            select(User.user_id).where(func.lower(User.email) == normalized)
        )
    raise ValueError("必须提供 email 或 user_id")


async def collect_usage_stats(
    sessions: async_sessionmaker[AsyncSession],
    *,
    email: str | None = None,
    user_id: UUID | None = None,
) -> AccountUsageStats | None:
    """在只读快照内汇总指定账号的使用统计；账号不存在时返回 None。"""
    async with read_snapshot(sessions) as session:
        resolved = await _resolve_user_id(session, email=email, user_id=user_id)
        if resolved is None:
            return None
        return AccountUsageStats(
            conversations=await _conversation_stats(session, resolved),
            grounding=await grounding_counts(session, resolved),
            edits=await _bullet_edit_counts(session, resolved),
            applications=await _application_stats(session, resolved),
            model_usage=await _model_usage_stats(session, resolved),
        )


def stats_to_dict(stats: AccountUsageStats) -> dict[str, Any]:
    """转为纯数字字典，不包含任何账号标识或业务正文。"""
    return {
        "conversations": {
            "total": stats.conversations.total_conversations,
            "completed_jd_to_resume_flows": (
                stats.conversations.completed_jd_to_resume_flows
            ),
            "dialogue_tailor_runs": stats.conversations.dialogue_tailor_runs,
        },
        "tailored_resumes": {
            "resume_count": stats.grounding.resume_count,
            "total_bullets": stats.grounding.total,
            "passed": stats.grounding.passed,
            "fallback": stats.grounding.fallback,
            "pending": stats.grounding.pending,
            "rejected": stats.grounding.rejected,
        },
        "manual_edits": {
            "resumes_with_edits": stats.edits.resumes_with_edits,
            "edited_bullets": stats.edits.edited_bullets,
            "use_facts_bullets": stats.edits.use_facts_bullets,
            "removed_bullets": stats.edits.removed_bullets,
        },
        "applications": {
            "total": stats.applications.total,
            "by_stage": dict(stats.applications.by_stage),
        },
        "model_usage": {
            "total_calls": stats.model_usage.total_calls,
            "succeeded_calls": stats.model_usage.succeeded_calls,
            "unknown_usage_calls": stats.model_usage.unknown_usage_calls,
            "prompt_tokens": stats.model_usage.prompt_tokens,
            "completion_tokens": stats.model_usage.completion_tokens,
            "total_tokens": stats.model_usage.total_tokens,
        },
    }


def format_usage_stats(stats: AccountUsageStats) -> str:
    """格式化纯数字统计摘要，供终端打印。"""
    c = stats.conversations
    g = stats.grounding
    e = stats.edits
    a = stats.applications
    m = stats.model_usage
    stage_labels = {
        ApplicationStage.APPLIED.value: "已投递",
        ApplicationStage.WRITTEN_TEST.value: "笔试",
        ApplicationStage.INTERVIEWING.value: "面试中",
        ApplicationStage.OFFER.value: "Offer",
        ApplicationStage.REJECTED.value: "已拒绝",
    }
    stage_summary = " / ".join(
        f"{stage_labels[s]} {a.by_stage.get(s, 0)}" for s in stage_labels
    )
    lines = [
        f"对话数：{c.total_conversations}",
        f"对话完成（贴 JD → 生成定制简历）：{c.completed_jd_to_resume_flows}",
        f"对话内成功改写次数：{c.dialogue_tailor_runs}",
        f"定制简历份数：{g.resume_count}",
        (
            f"改写条目数：{g.total}（通过 {g.passed} / 回退 {g.fallback} / "
            f"待处理 {g.pending} / 改用事实原文标记 {g.rejected}）"
        ),
        (
            f"版本差异（当前版对比 v1）：有改动的简历 {e.resumes_with_edits} 份，"
            f"手动改字条目 {e.edited_bullets}，改用事实原文条目 {e.use_facts_bullets}，"
            f"删除条目 {e.removed_bullets}"
        ),
        f"记录投递数：{a.total}（{stage_summary}）",
        (
            f"模型调用次数：{m.total_calls}（成功 {m.succeeded_calls} / "
            f"未知用量 {m.unknown_usage_calls}）"
        ),
        (
            f"实际 token 用量：输入 {m.prompt_tokens}，输出 {m.completion_tokens}，"
            f"合计 {m.total_tokens}"
        ),
    ]
    return "\n".join(lines)
