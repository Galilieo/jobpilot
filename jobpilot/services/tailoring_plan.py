"""改写计划：决定改写哪几块、按什么顺序、每块突出哪些岗位要求。纯函数，不调用模型。

设计（06 T2.4）：程序兜住下限，模型提高上限。
- 分块：实习、项目、其他经历每段一块；技能概述单独一块，可以引用各经历里的技术事实。
  教育和获奖不改写，由组装时从底稿原样保留。
- 默认保留全部经历，只给建议顺序和要突出的要求；每块写什么由模型在这块的事实里挑。
- 每条要突出的要求，按所选底稿判断「已写到 / 建议补入 / 待核对」（评审 2026-11-24）：
  匹配报告只说明事实能不能支撑，底稿有没有写要等选定底稿后才知道。
"""

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel

from jobpilot.db.models import (
    BulletSection,
    Fact,
    FactKind,
    FactSource,
    SectionKind,
)
from jobpilot.services.grounding import GroundingFact
from jobpilot.services.jobs import Requirement, RequirementType
from jobpilot.services.matching import MatchItem

MAX_BULLETS_PER_BLOCK = 4
# 逐段改写的经历，也是计划里各类的先后顺序；教育和获奖不改写。
REWRITTEN_SECTIONS = (SectionKind.INTERNSHIP, SectionKind.PROJECT, SectionKind.OTHER)
SKILLS_KEY = "skills"


class UnresolvedSectionsError(ValueError):
    """有经历推断不出段落类型，需要用户指定。"""

    def __init__(self, experiences: Sequence[str]) -> None:
        super().__init__("无法确定这些经历属于哪一栏：" + "、".join(experiences))
        self.experiences = tuple(experiences)


@dataclass(frozen=True)
class TailorFact:
    """改写要用的事实：依据检查的字段，加上分块和判断底稿要用的字段。"""

    grounding: GroundingFact
    kind: FactKind
    source_doc_id: UUID | None
    source_quote: str | None

    @classmethod
    def of(cls, fact: Fact) -> "TailorFact":
        return cls(
            grounding=GroundingFact.of(fact),
            kind=FactKind(fact.kind),
            source_doc_id=fact.source_doc_id,
            source_quote=fact.source_quote,
        )

    @property
    def fact_id(self) -> UUID:
        return self.grounding.fact_id

    @property
    def experience(self) -> str:
        return self.grounding.experience


class BaseDocStatus(StrEnum):
    """这条要求的依据在所选底稿里写了没有。"""

    WRITTEN = "written"  # 底稿里有
    SUGGEST_ADD = "suggest_add"  # 别的简历里写过、经原文校验，这份底稿没写：建议补入
    TO_CHECK = "to_check"  # 用户补充的事实，底稿里找不到原样文字：可能换了说法，待核对


class FocusRequirement(BaseModel):
    requirement_id: UUID
    type: RequirementType
    text: str
    base_status: BaseDocStatus


class PlanBlock(BaseModel):
    """一次改写调用：一段经历，或技能概述。"""

    key: str
    section: SectionKind  # 技能概述为 skill
    bullet_section: BulletSection
    experience: str | None  # 技能概述为 None
    fact_ids: tuple[UUID, ...]
    focus: tuple[FocusRequirement, ...]
    max_bullets: int = MAX_BULLETS_PER_BLOCK


# ---------- 段落类型 ----------


def resolve_sections(
    facts: Sequence[TailorFact],
    overrides: Mapping[str, SectionKind] | None = None,
) -> dict[str, SectionKind]:
    """每段经历的段落类型：同一段经历里带段落类型的事实说了算。

    旧版本和用户补充的事实可能没有段落类型；
    同一段经历里只要别的事实有、且只有一种，就用它。
    一种都没有，或者有两种对不上，都要用户在 overrides 里指定，
    否则抛 UnresolvedSectionsError。
    """
    overrides = overrides or {}
    seen: dict[str, set[SectionKind]] = {}
    for fact in facts:
        kinds = seen.setdefault(fact.experience, set())
        if fact.grounding.section is not None:
            kinds.add(fact.grounding.section)

    resolved: dict[str, SectionKind] = {}
    unresolved = []
    for experience, kinds in seen.items():
        if experience in overrides:
            resolved[experience] = overrides[experience]
        elif len(kinds) == 1:
            resolved[experience] = next(iter(kinds))
        else:
            unresolved.append(experience)
    if unresolved:
        raise UnresolvedSectionsError(unresolved)
    return resolved


# ---------- 底稿里写了没有 ----------


def _compact(text: str) -> str:
    """比较前去掉空白、统一全半角：「Go 和 Redis」与「Go和Redis」算同一段文字。"""
    return "".join(unicodedata.normalize("NFKC", text).split())


def base_doc_status(
    cited: Sequence[TailorFact], *, base_doc_id: UUID, base_text: str
) -> BaseDocStatus:
    """引用的事实里有一条在底稿里，就算已写到。

    出自这份底稿且原文校验通过的，直接算写到；其他的看原文片段或事实文字是否原样出现在底稿里。
    只按原样比较：换了说法的判断不了，用户补充的事实因此标「待核对」，不冒充「已写到」。
    """
    base = _compact(base_text)
    for fact in cited:
        if fact.source_doc_id == base_doc_id and fact.grounding.quote_verified:
            return BaseDocStatus.WRITTEN
        written = (fact.source_quote, fact.grounding.text)
        if any(text and _compact(text) in base for text in written):
            return BaseDocStatus.WRITTEN
    from_other_resume = all(
        fact.grounding.source is FactSource.RESUME and fact.grounding.quote_verified
        for fact in cited
    )
    return BaseDocStatus.SUGGEST_ADD if from_other_resume else BaseDocStatus.TO_CHECK


# ---------- 计划 ----------


_TYPE_WEIGHT = {RequirementType.HARD: 2, RequirementType.PREFERRED: 1}
_STATUS_ORDER = {
    BaseDocStatus.SUGGEST_ADD: 0,  # 最值得写进去的放前面
    BaseDocStatus.TO_CHECK: 1,
    BaseDocStatus.WRITTEN: 2,
}


def _focus_for(
    block_fact_ids: set[UUID],
    supporting: Sequence[tuple[Requirement, MatchItem]],
    statuses: Mapping[UUID, BaseDocStatus],
) -> tuple[FocusRequirement, ...]:
    """这块的事实支撑了哪些要求：硬性要求在前，同类里底稿没写到的在前。"""
    focus = [
        FocusRequirement(
            requirement_id=requirement.requirement_id,
            type=requirement.type,
            text=requirement.text,
            base_status=statuses[requirement.requirement_id],
        )
        for requirement, item in supporting
        if block_fact_ids.intersection(item.fact_ids)
    ]
    focus.sort(key=lambda f: (-_TYPE_WEIGHT[f.type], _STATUS_ORDER[f.base_status]))
    return tuple(focus)


def _score(block: PlanBlock) -> int:
    return sum(_TYPE_WEIGHT[focus.type] for focus in block.focus)


def build_plan(
    facts: Sequence[TailorFact],
    requirements: Sequence[Requirement],
    match_items: Sequence[MatchItem],
    *,
    base_doc_id: UUID,
    base_text: str,
    section_overrides: Mapping[str, SectionKind] | None = None,
) -> list[PlanBlock]:
    """按段落分块、排顺序、标出要突出的要求。

    顺序：实习、项目、其他依次排；同一类里，支撑的要求越多越靠前
    （硬性要求算 2、加分项算 1），一样多时保持事实库里的顺序。技能概述放最后。
    只是建议，不删经历。
    """
    sections = resolve_sections(facts, section_overrides)
    by_id = {fact.fact_id: fact for fact in facts}
    requirement_by_id = {r.requirement_id: r for r in requirements}
    # 引用了事实的要求（已覆盖、表达缺口、部分覆盖）才值得突出；未覆盖的不引用事实，
    # 不交给模型去凑。
    supporting = [
        (requirement_by_id[item.requirement_id], item)
        for item in match_items
        if item.fact_ids and item.requirement_id in requirement_by_id
    ]
    statuses = {
        requirement.requirement_id: base_doc_status(
            [by_id[i] for i in item.fact_ids if i in by_id],
            base_doc_id=base_doc_id,
            base_text=base_text,
        )
        for requirement, item in supporting
    }

    experience_facts: dict[str, list[UUID]] = {}
    for fact in facts:
        if sections[fact.experience] in REWRITTEN_SECTIONS:
            experience_facts.setdefault(fact.experience, []).append(fact.fact_id)

    blocks = [
        PlanBlock(
            key=f"experience:{experience}",
            section=sections[experience],
            bullet_section=BulletSection.EXPERIENCE,
            experience=experience,
            fact_ids=tuple(ids),
            focus=_focus_for(set(ids), supporting, statuses),
        )
        for experience, ids in experience_facts.items()
    ]
    # sort 是稳定的：分数相同时保持原顺序。
    blocks.sort(key=lambda b: (REWRITTEN_SECTIONS.index(b.section), -_score(b)))

    skill_ids = tuple(
        fact.fact_id
        for fact in facts
        if sections[fact.experience] is SectionKind.SKILL
        or (
            sections[fact.experience] in REWRITTEN_SECTIONS
            and fact.kind is FactKind.TECHNOLOGY
        )
    )
    if skill_ids:
        blocks.append(
            PlanBlock(
                key=SKILLS_KEY,
                section=SectionKind.SKILL,
                bullet_section=BulletSection.SKILLS,
                experience=None,
                fact_ids=skill_ids,
                focus=_focus_for(set(skill_ids), supporting, statuses),
            )
        )
    return blocks
