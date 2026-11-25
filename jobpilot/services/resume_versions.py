"""完整简历的读取与保存编辑：每次保存新增一个版本，旧版本保留（01 F5，06 T2.5）。

保存时客户端发来整份内容，但能决定依据的部分一律以服务端为准：
- 每条改写条目引用哪些事实、生成时的结果，按 bullet_id 从上一版取，客户端改不了；
- 不认识的条目或经历（客户端编出来的 ID）、重复的条目都拒绝；
- 文字改过的条目在保存时重新做依据检查第一、二层，结论绑定新文字；
  没改过的沿用原来的结论。检查只提示，不阻止保存。
页头、标题行、没改写的正文可以自由修改，不做依据检查。
并发：锁住简历行，核对 base_version_id 是否仍是当前版本，不是就拒绝（409），
客户端保留自己的草稿。保存交回的是这次建立的那一版，不是「提交之后再读一次当前版本」：
读的那一瞬间，别的窗口可能已经又存了一版（评审 2026-11-25）。
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.db.models import (
    BulletSection,
    ResumeVersion,
    SectionKind,
    TailoredResume,
)
from jobpilot.services.grounding import GroundingFact, check_grounding
from jobpilot.services.resume_content import (
    Bullet,
    BulletCheck,
    CheckLayers,
    Entry,
    ResumeContent,
    plain,
)


class ResumeNotFoundError(LookupError):
    """运行不存在、不属于该用户、没有成功，或请求的版本不存在。"""


class VersionConflictError(RuntimeError):
    """保存时依据的版本已经不是当前版本。"""

    def __init__(self, current_version_id: UUID) -> None:
        super().__init__("简历已被别处修改")
        self.current_version_id = current_version_id


class UnknownPartError(ValueError):
    """提交的内容里有上一版没有的条目或经历，或条目重复。"""

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__("；".join(problems))
        self.problems = tuple(problems)


# ---------- 保存前重新核对 ----------


def recheck(
    previous: ResumeContent,
    submitted: ResumeContent,
    available: Mapping[UUID, GroundingFact],
    organizations: Iterable[str],
) -> ResumeContent:
    """以上一版为准补回依据字段，文字改过的条目重新检查第一、二层。"""
    entries = {e.entry_id: e for s in previous.sections for e in s.entries}
    before = {b.bullet_id: b for e in entries.values() for b in e.bullets}
    _reject_unknown(submitted, entries, before)
    organizations = frozenset(organizations)

    sections = []
    for section in submitted.sections:
        checked = []
        for entry in section.entries:
            experience = entries[entry.entry_id].experience
            bullets = [
                _recheck_bullet(
                    sent,
                    before[sent.bullet_id],
                    section.kind,
                    experience,
                    available,
                    organizations,
                )
                for sent in entry.bullets
            ]
            checked.append(
                entry.model_copy(update={"experience": experience, "bullets": bullets})
            )
        sections.append(section.model_copy(update={"entries": checked}))
    return ResumeContent(
        header=submitted.header, sections=sections, style=submitted.style
    )


def _reject_unknown(
    submitted: ResumeContent,
    entries: Mapping[str, Entry],
    before: Mapping[UUID, Bullet],
) -> None:
    problems = []
    sent_entries = [e for s in submitted.sections for e in s.entries]
    sent_bullets = [b.bullet_id for e in sent_entries for b in e.bullets]
    unknown_entries = [e.entry_id for e in sent_entries if e.entry_id not in entries]
    if unknown_entries:
        problems.append("不认识的经历：" + "、".join(unknown_entries))
    unknown_bullets = [str(b) for b in sent_bullets if b not in before]
    if unknown_bullets:
        problems.append("不认识的条目：" + "、".join(unknown_bullets))
    if len(set(sent_bullets)) != len(sent_bullets):
        problems.append("同一条目出现了不止一次")
    if problems:
        raise UnknownPartError(problems)


def _recheck_bullet(
    sent: Bullet,
    before: Bullet,
    kind: SectionKind,
    experience: str | None,
    available: Mapping[UUID, GroundingFact],
    organizations: frozenset[str],
) -> Bullet:
    text = sent.text.strip()
    if plain(text) == plain(before.check.text):
        check = before.check  # 只改了加粗，结论不变
    else:
        skills = kind is SectionKind.SKILL
        issues = check_grounding(
            plain(text),
            before.fact_ids,
            available,
            section=BulletSection.SKILLS if skills else BulletSection.EXPERIENCE,
            experience=None if skills else experience,
            known_organizations=organizations,
        ).issues
        check = BulletCheck(text=text, layers=CheckLayers.PROGRAM, issues=list(issues))
    return Bullet(
        bullet_id=before.bullet_id,
        text=text,
        fact_ids=before.fact_ids,
        origin=before.origin,
        edited=before.edited or plain(text) != plain(before.text),
        check=check,
    )


# ---------- 读取与保存 ----------


@dataclass(frozen=True)
class LoadedResume:
    resume: TailoredResume
    version: ResumeVersion  # 请求的版本，默认当前版本
    history: list[ResumeVersion]  # 全部版本，按版本号排

    @property
    def content(self) -> ResumeContent:
        return ResumeContent.model_validate(self.version.content_json)


async def load_resume(
    db: AsyncSession, user_id: UUID, run_id: UUID, version: int | None = None
) -> LoadedResume:
    resume = await _resume_of(db, user_id, run_id)
    history = await _history(db, user_id, resume.resume_id)
    if version is None:
        wanted = [
            r for r in history if r.resume_version_id == resume.current_version_id
        ]
    else:
        wanted = [r for r in history if r.version == version]
    if not wanted:
        raise ResumeNotFoundError(run_id)
    return LoadedResume(resume=resume, version=wanted[0], history=history)


async def save_version(
    db: AsyncSession,
    user_id: UUID,
    run_id: UUID,
    *,
    base_version_id: UUID,
    submitted: ResumeContent,
    available: Mapping[UUID, GroundingFact],
    organizations: Iterable[str],
) -> LoadedResume:
    """新增一个版本并指向它，连同简历和历史一起交回；调用方提交事务。

    交回的版本就是这次保存建立的那一版，历史也只到这里：还持着简历行上的锁，
    别的保存要等调用方提交之后才能进来。回包必须在提交前用它组装好。
    """
    resume = await _resume_of(db, user_id, run_id, for_update=True)
    if resume.current_version_id != base_version_id:
        raise VersionConflictError(resume.current_version_id)
    current = await db.get_one(ResumeVersion, resume.current_version_id)
    content = recheck(
        ResumeContent.model_validate(current.content_json),
        submitted,
        available,
        organizations,
    )
    latest = await db.scalar(
        select(func.max(ResumeVersion.version)).where(
            ResumeVersion.resume_id == resume.resume_id
        )
    )
    row = ResumeVersion(
        resume_version_id=uuid4(),
        resume_id=resume.resume_id,
        user_id=user_id,
        version=(latest or 0) + 1,
        content_json=content.model_dump(mode="json"),
    )
    db.add(row)
    resume.current_version_id = row.resume_version_id
    await db.flush()
    return LoadedResume(
        resume=resume,
        version=row,
        history=await _history(db, user_id, resume.resume_id),
    )


async def _history(
    db: AsyncSession, user_id: UUID, resume_id: UUID
) -> list[ResumeVersion]:
    """这份简历的全部版本，按版本号排。"""
    return list(
        (
            await db.scalars(
                select(ResumeVersion)
                .where(
                    ResumeVersion.resume_id == resume_id,
                    ResumeVersion.user_id == user_id,
                )
                .order_by(ResumeVersion.version)
            )
        ).all()
    )


async def _resume_of(
    db: AsyncSession, user_id: UUID, run_id: UUID, *, for_update: bool = False
) -> TailoredResume:
    query = select(TailoredResume).where(
        TailoredResume.run_id == run_id, TailoredResume.user_id == user_id
    )
    if for_update:
        query = query.with_for_update()
    resume = await db.scalar(query)
    if resume is None:
        raise ResumeNotFoundError(run_id)
    return resume
