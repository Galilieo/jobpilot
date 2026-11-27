"""按稳定的条目与事实标识，对比投递快照和当前数据。"""

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.db import applications as records
from jobpilot.db.models import Application, Fact
from jobpilot.services import drafts
from jobpilot.services.applications import ApplicationError, require_application
from jobpilot.services.resume_content import Bullet, ResumeContent, UserBulletChoice


class TextChange(BaseModel):
    item_id: UUID
    before: str | None
    after: str | None


class RiskReason(StrEnum):
    FACT_CHANGED = "fact_changed"
    FACT_REMOVED = "fact_removed"
    BULLET_REMOVED = "bullet_removed"
    USES_FACTS = "uses_facts"


class ApplicationRisk(BaseModel):
    bullet_id: UUID
    bullet_text: str
    reason: RiskReason
    facts: list[TextChange]


class ApplicationDiff(BaseModel):
    source_deleted: bool
    regenerated: bool = False
    regenerated_resume_id: UUID | None = None
    regenerated_content: ResumeContent | None = None
    resume_changes: list[TextChange] = Field(default_factory=list)
    fact_changes: list[TextChange] = Field(default_factory=list)
    risks: list[ApplicationRisk] = Field(default_factory=list)


def _bullet_index(content: ResumeContent) -> dict[UUID, Bullet]:
    return {
        bullet.bullet_id: bullet
        for section in content.sections
        for entry in section.entries
        for bullet in entry.bullets
    }


def _text_changes(before: dict[UUID, str], after: dict[UUID, str]) -> list[TextChange]:
    return [
        TextChange(item_id=key, before=before.get(key), after=after.get(key))
        for key in dict.fromkeys([*before, *after])
        if before.get(key) != after.get(key)
    ]


def _fact_changes(previous: list[Fact], current: list[Fact]) -> list[TextChange]:
    """以 lineage_id 对齐已发布事实，逐条比较用于求职的事实文本。"""
    return _text_changes(
        {fact.lineage_id: fact.text for fact in previous},
        {fact.lineage_id: fact.text for fact in current},
    )


def _bullet_risks(
    before: Bullet,
    after: Bullet | None,
    *,
    same_resume: bool,
    citations: list[TextChange],
    changed_facts: dict[UUID, TextChange],
) -> list[ApplicationRisk]:
    affected: dict[RiskReason, list[TextChange]] = {}
    for fact in citations:
        changed = changed_facts.get(fact.item_id)
        if changed is not None:
            reason = (
                RiskReason.FACT_REMOVED
                if changed.after is None
                else RiskReason.FACT_CHANGED
            )
            affected.setdefault(reason, []).append(changed)
    if same_resume and after is None:
        affected[RiskReason.BULLET_REMOVED] = citations
    elif (
        after is not None
        and after.user_choice == UserBulletChoice.USE_FACTS
        and before.user_choice != UserBulletChoice.USE_FACTS
    ):
        affected[RiskReason.USES_FACTS] = citations
    return [
        ApplicationRisk(
            bullet_id=before.bullet_id,
            bullet_text=before.text,
            reason=reason,
            facts=facts,
        )
        for reason, facts in affected.items()
    ]


async def _compare_resume(
    user_id: UUID,
    session: AsyncSession,
    row: Application,
    before: dict[UUID, Bullet],
) -> tuple[ApplicationDiff, dict[UUID, Bullet]]:
    current = (
        await records.current_resume_version(user_id, session, row.resume_id)
        if row.resume_version_id is not None and row.resume_id is not None
        else None
    )
    result = ApplicationDiff(source_deleted=current is None)
    if current is None:
        return result, {}
    after = _bullet_index(ResumeContent.from_stored(current.content_json))
    result.resume_changes = _text_changes(
        {key: bullet.text for key, bullet in before.items()},
        {key: bullet.text for key, bullet in after.items()},
    )
    if row.user_job_id is not None:
        latest = await records.latest_job_resume(user_id, session, row.user_job_id)
        if latest is not None and latest[1].resume_id != row.resume_id:
            selected = await records.find_resume_version(
                user_id, session, row.resume_version_id
            )
            if selected is not None and (latest[1].created_at, latest[1].resume_id) > (
                selected[1].created_at,
                selected[1].resume_id,
            ):
                result.regenerated = True
                result.regenerated_resume_id = latest[1].resume_id
                result.regenerated_content = ResumeContent.from_stored(
                    latest[0].content_json
                )
    return result, after


async def compare_application(
    user_id: UUID, session: AsyncSession, application_id: UUID
) -> ApplicationDiff:
    row = await require_application(user_id, session, application_id)
    if row.snapshot_json is None or row.fact_set_id is None:
        raise ApplicationError(404, "这条投递没有可对比的简历快照")
    before = _bullet_index(ResumeContent.from_stored(row.snapshot_json))
    result, after = await _compare_resume(user_id, session, row, before)
    original_facts = await drafts.facts_in(session, user_id, row.fact_set_id)
    latest_facts = await drafts.facts_of(
        session, await drafts.latest_fact_set(session, user_id)
    )
    result.fact_changes = _fact_changes(original_facts, latest_facts)
    changed = {change.item_id: change for change in result.fact_changes}
    latest_by_lineage = {fact.lineage_id: fact.text for fact in latest_facts}
    citations = {
        fact.fact_id: TextChange(
            item_id=fact.lineage_id,
            before=fact.text,
            after=latest_by_lineage.get(fact.lineage_id),
        )
        for fact in original_facts
    }
    for key, bullet in before.items():
        result.risks.extend(
            _bullet_risks(
                bullet,
                after.get(key),
                same_resume=not result.source_deleted,
                citations=[
                    citations[fact_id]
                    for fact_id in bullet.fact_ids
                    if fact_id in citations
                ],
                changed_facts=changed,
            )
        )
    return result
