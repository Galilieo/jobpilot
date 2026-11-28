"""读取账号导出所需的业务记录；每组记录都按账号过滤。"""

from typing import Any
from uuid import UUID

from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.db.models import (
    AgentAction,
    AiRun,
    Application,
    ApplicationStageEvent,
    BulletFact,
    Conversation,
    Fact,
    FactDraft,
    FactSet,
    FactSetCriterion,
    FactSetEducation,
    JobPosting,
    MatchReport,
    Memory,
    Message,
    ResumeDocument,
    ResumeVersion,
    TailoredBullet,
    TailoredResume,
    User,
    UserJob,
    WritingPreference,
)

# 只列用户拥有的业务表，会话、幂等响应和模型调用记录没有导出入口。
_EXPORT_TABLES = {
    "fact_sets": FactSet.__table__,
    "facts": Fact.__table__,
    "fact_set_criteria": FactSetCriterion.__table__,
    "fact_set_educations": FactSetEducation.__table__,
    "fact_drafts": FactDraft.__table__,
    "resume_documents": ResumeDocument.__table__,
    "job_postings": JobPosting.__table__,
    "user_jobs": UserJob.__table__,
    "match_reports": MatchReport.__table__,
    "ai_runs": AiRun.__table__,
    "conversations": Conversation.__table__,
    "messages": Message.__table__,
    "memory": Memory.__table__,
    "writing_preference": WritingPreference.__table__,
    "agent_actions": AgentAction.__table__,
    "tailored_bullets": TailoredBullet.__table__,
    "bullet_facts": BulletFact.__table__,
    "tailored_resumes": TailoredResume.__table__,
    "resume_versions": ResumeVersion.__table__,
    "applications": Application.__table__,
    "application_stage_events": ApplicationStageEvent.__table__,
}
_VERSION_ORDER = {
    Message.__table__: (Message.seq,),
    ApplicationStageEvent.__table__: (
        ApplicationStageEvent.created_at,
        ApplicationStageEvent.event_id,
    ),
    FactSet.__table__: (FactSet.version,),
    ResumeVersion.__table__: (ResumeVersion.resume_id, ResumeVersion.version),
}


async def export_identity(
    session: AsyncSession, user_id: UUID
) -> dict[str, Any] | None:
    """账号表只读取可导出的身份字段。"""
    identity = (
        (
            await session.execute(
                select(
                    User.user_id,
                    User.email,
                    User.created_at,
                    User.deletion_requested_at,
                ).where(User.user_id == user_id)
            )
        )
        .mappings()
        .one_or_none()
    )
    return dict(identity) if identity is not None else None


async def export_business_records(
    session: AsyncSession, user_id: UUID
) -> dict[str, list[dict[str, Any]]]:
    return {
        name: await _owned_rows(session, user_id, table)
        for name, table in _EXPORT_TABLES.items()
    }


async def _owned_rows(
    session: AsyncSession, user_id: UUID, table: Table
) -> list[dict[str, Any]]:
    owner = table.c.owner_id if table is JobPosting.__table__ else table.c.user_id
    columns = [
        column
        for column in table.c
        if column.key not in {"thread_id", "content_hash", "snapshot_html"}
    ]
    ordering = _VERSION_ORDER.get(table, tuple(table.primary_key))
    rows = await session.execute(
        select(*columns).where(owner == user_id).order_by(*ordering)
    )
    items = [dict(row) for row in rows.mappings()]
    if table is Message.__table__:
        from jobpilot.services.agent.step_details import strip_search_step_excerpts

        for item in items:
            if isinstance(item.get("steps_json"), list):
                item["steps_json"] = strip_search_step_excerpts(item["steps_json"])
    return items
