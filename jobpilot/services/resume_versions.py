"""完整简历的读取、保存编辑与重置（01 F5，06 T2.5）。

每次保存或重置都新增一个版本，旧版本保留。

保存时客户端发来整份内容，但能决定依据的部分一律以服务端为准：
- 每条改写条目引用哪些事实、生成时的结果，按 bullet_id 从上一版取，客户端改不了；
- 不认识的条目或经历（客户端编出来的 ID）、重复的条目都拒绝；
- 文字改过的条目在保存时重新做依据检查第一、二层，结论绑定新文字；
  没改过的沿用原来的结论。检查只提示，不阻止保存。
- 沿用的结论是在原来的经历、原来的栏目下得出的，所以条目不能换所属经历，经历不能
  重复出现、不能换栏目，换了就拒绝；检查用的栏目类型和经历名也一律取上一版的。
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
    """提交的内容里有上一版没有的条目或经历、重复的条目，或者条目和经历换了位置。"""

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
    # 上一版里每段经历所在的栏目、每条条目所属的经历：检查结论和它们绑在一起
    kinds = {e.entry_id: s.kind for s in previous.sections for e in s.entries}
    owners = {
        b.bullet_id: e.entry_id
        for s in previous.sections
        for e in s.entries
        for b in e.bullets
    }
    _reject_unknown(submitted, entries, before)
    _reject_moved(submitted, kinds, owners)
    organizations = frozenset(organizations)

    sections = []
    for section in submitted.sections:
        checked = []
        for entry in section.entries:
            experience = entries[entry.entry_id].experience
            kind = kinds[entry.entry_id]  # 检查按原来的栏目做，不看请求里的
            bullets = [
                _recheck_bullet(
                    sent,
                    before[sent.bullet_id],
                    kind,
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


def _reject_moved(
    submitted: ResumeContent,
    kinds: Mapping[str, SectionKind],
    owners: Mapping[UUID, str],
) -> None:
    """换了位置的条目和经历拒绝保存。

    文字没变的条目沿用上一版的检查结论，而那个结论是在原来的经历、原来的栏目下得出的：
    实习里通过的条目挪到项目名下，就成了项目里「已通过」的成果。编辑器只能原位改字，
    所以换了所属经历、同一经历出现两次、经历换了栏目，都按不认识的内容处理。
    """
    problems = []
    seen: set[str] = set()
    for section in submitted.sections:
        for entry in section.entries:
            if entry.entry_id in seen:
                problems.append(f"同一经历出现了不止一次：{entry.entry_id}")
            seen.add(entry.entry_id)
            # 不认识的条目和经历由 _reject_unknown 报告，这里不算换了位置。
            if kinds.get(entry.entry_id, section.kind) is not section.kind:
                problems.append(f"经历换了栏目：{entry.entry_id}")
            moved = [
                str(b.bullet_id)
                for b in entry.bullets
                if owners.get(b.bullet_id, entry.entry_id) != entry.entry_id
            ]
            if moved:
                problems.append("条目换了所属经历：" + "、".join(moved))
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
    """保存客户端发来的编辑，新增一个版本并指向它；调用方提交事务。

    交回的版本就是这次建立的那一版，历史也只到这里：还持着简历行上的锁，
    别的保存要等调用方提交之后才能进来。回包必须在提交前用它组装好。
    """
    resume = await _locked_resume(db, user_id, run_id, base_version_id)
    current = await db.get_one(ResumeVersion, resume.current_version_id)
    content = recheck(
        ResumeContent.model_validate(current.content_json),
        submitted,
        available,
        organizations,
    )
    return await _append_version(db, user_id, resume, content)


async def reset_version(
    db: AsyncSession, user_id: UUID, run_id: UUID, *, base_version_id: UUID
) -> LoadedResume:
    """把系统生成的第一版原样复制成一个新版本并指向它；调用方提交事务。

    当前版本里删掉的条目，第一版里还有：让客户端读出第一版再当普通编辑保存，
    会因为这些条目「上一版里没有」而被拒绝。所以由服务端复制，不经保存前的核对。
    """
    resume = await _locked_resume(db, user_id, run_id, base_version_id)
    first = await db.scalar(
        select(ResumeVersion).where(
            ResumeVersion.resume_id == resume.resume_id,
            ResumeVersion.user_id == user_id,
            ResumeVersion.version == 1,
        )
    )
    if first is None:
        raise ResumeNotFoundError(run_id)
    return await _append_version(
        db, user_id, resume, ResumeContent.model_validate(first.content_json)
    )


async def _locked_resume(
    db: AsyncSession, user_id: UUID, run_id: UUID, base_version_id: UUID
) -> TailoredResume:
    """保存、重置这类编辑动作的共同起点：锁住简历行，核对请求依据的还是当前版本。"""
    resume = await _resume_of(db, user_id, run_id, for_update=True)
    if resume.current_version_id != base_version_id:
        raise VersionConflictError(resume.current_version_id)
    return resume


async def _append_version(
    db: AsyncSession, user_id: UUID, resume: TailoredResume, content: ResumeContent
) -> LoadedResume:
    """用这份内容建立下一版、移动指针，连同简历和历史交回；锁仍由调用方持有。"""
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
