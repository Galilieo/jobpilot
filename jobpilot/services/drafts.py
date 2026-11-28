"""事实库草稿与发布：确认、修改、补充、删除都先进草稿，发布时生成新版本。

每个用户一份草稿（fact_draft 一行），条目放在 items_json。
条目用 lineage_id 指代一条事实：已发布的事实沿用原有的 lineage_id，
草稿里的新事实在创建时分配。

这里的函数只改 session 里的对象，不提交；调用方在同一个事务里提交。
修改草稿前都先锁住草稿行（SELECT ... FOR UPDATE），同一用户的并发修改排队执行。
"""

from collections.abc import Sequence
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.db.models import (
    INTERNSHIP_MONTHS_MAX,
    INTERNSHIP_MONTHS_MIN,
    ONSITE_DAYS_MAX,
    ONSITE_DAYS_MIN,
    PREFERRED_CITY_CHARS,
    PREFERRED_CITY_COUNT,
    TARGET_DIRECTION_CHARS,
    CriterionField,
    DegreeLevel,
    EducationStatus,
    Fact,
    FactDraft,
    FactKind,
    FactSet,
    FactSetCriterion,
    FactSetEducation,
    FactSource,
    Responsibility,
    SectionKind,
    User,
)
from jobpilot.services.facts import (
    RESPONSIBILITY_SECTIONS,
    DraftFact,
    ExtractedCriterionDraft,
    ExtractedEducationDraft,
    ReviewReason,
    contains_number,
)


class ItemStatus(StrEnum):
    """草稿条目的状态。只有 confirmed 和 deleted 会在发布时生效。"""

    PENDING = "pending"  # 待确认，发布后仍留在草稿
    CONFIRMED = "confirmed"  # 已确认，进入下一个版本
    DELETED = "deleted"  # 已发布的事实，下一个版本移除


_RESPONSIBILITY_REASONS = frozenset(
    {ReviewReason.RESPONSIBILITY_UNSTATED, ReviewReason.RESPONSIBILITY_UNSUPPORTED}
)


class DraftItem(BaseModel):
    """草稿里的一条事实。字段与 fact 表对应，另加状态、段落类型和核对原因。"""

    lineage_id: UUID
    status: ItemStatus
    experience: str
    section: SectionKind | None = None  # 用户补充的事实没有段落类型
    kind: FactKind
    responsibility: Responsibility
    text: str
    has_number: bool
    source: FactSource
    source_doc_id: UUID | None = None
    source_quote: str | None = None
    # 片段在 raw_text 中的位置，按 Python 字符计数；前端高亮要换算成 UTF-16。
    span_start: int | None = None
    span_end: int | None = None
    quote_verified: bool = False
    review_reasons: list[ReviewReason] = Field(default_factory=list)

    @classmethod
    def from_extracted(cls, fact: DraftFact) -> "DraftItem":
        """模型拆出的事实：分配新的 lineage_id，等用户确认。"""
        return cls(
            lineage_id=uuid4(),
            status=ItemStatus.PENDING,
            source=FactSource.RESUME,
            **fact.model_dump(),
        )

    @classmethod
    def from_published(cls, fact: Fact, status: ItemStatus) -> "DraftItem":
        """已发布的事实复制进草稿，再在草稿里修改或标记删除。"""
        return cls(
            lineage_id=fact.lineage_id,
            status=status,
            experience=fact.experience,
            section=SectionKind(fact.section) if fact.section else None,
            kind=FactKind(fact.kind),
            responsibility=Responsibility(fact.responsibility),
            text=fact.text,
            has_number=fact.has_number,
            source=FactSource(fact.source),
            source_doc_id=fact.source_doc_id,
            source_quote=fact.source_quote,
            span_start=fact.span_start,
            span_end=fact.span_end,
            quote_verified=fact.quote_verified,
        )

    @property
    def blocked(self) -> bool:
        """简历来源却没通过原文校验：不能确认，只能改写文字或删除。"""
        return self.source is FactSource.RESUME and not self.quote_verified

    def to_fact(self, fact_set_id: UUID, user_id: UUID) -> Fact:
        return Fact(
            fact_set_id=fact_set_id,
            user_id=user_id,
            lineage_id=self.lineage_id,
            experience=self.experience,
            section=self.section,
            kind=self.kind,
            responsibility=self.responsibility,
            text=self.text,
            has_number=self.has_number,
            source=self.source,
            source_doc_id=self.source_doc_id,
            source_quote=self.source_quote,
            span_start=self.span_start,
            span_end=self.span_end,
            quote_verified=self.quote_verified,
        )


class FactChanges(BaseModel):
    """一次修改；没给的字段保持不变。"""

    text: str | None = Field(default=None, min_length=1, max_length=500)
    kind: FactKind | None = None
    responsibility: Responsibility | None = None
    confirmed: bool | None = None


class DraftCriterion(BaseModel):
    """草稿里的一项求职条件，存入 fact_draft.criteria_json。"""

    field: CriterionField
    status: ItemStatus
    int_value: int | None = None
    text_value: str | None = None
    list_value: list[str] | None = None
    source: FactSource
    source_doc_id: UUID | None = None
    source_quote: str | None = None
    span_start: int | None = None
    span_end: int | None = None
    quote_verified: bool = False
    review_reasons: list[ReviewReason] = Field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.source is FactSource.RESUME and not self.quote_verified

    @classmethod
    def from_extracted(cls, item: ExtractedCriterionDraft) -> "DraftCriterion":
        return cls(
            status=ItemStatus.PENDING,
            source=FactSource.RESUME,
            **item.model_dump(),
        )

    @classmethod
    def from_published(
        cls, row: FactSetCriterion, status: ItemStatus
    ) -> "DraftCriterion":
        return cls(
            field=CriterionField(row.field),
            status=status,
            int_value=row.int_value,
            text_value=row.text_value,
            list_value=row.list_value,
            source=FactSource(row.source),
            source_doc_id=row.source_doc_id,
            source_quote=row.source_quote,
            span_start=row.span_start,
            span_end=row.span_end,
            quote_verified=row.quote_verified,
        )

    def to_row(self, fact_set_id: UUID, user_id: UUID) -> FactSetCriterion:
        return FactSetCriterion(
            fact_set_id=fact_set_id,
            user_id=user_id,
            field=self.field,
            int_value=self.int_value,
            text_value=self.text_value,
            list_value=self.list_value,
            source=self.source,
            source_doc_id=self.source_doc_id,
            source_quote=self.source_quote,
            span_start=self.span_start,
            span_end=self.span_end,
            quote_verified=self.quote_verified,
        )


class DraftEducation(BaseModel):
    """草稿里的一段教育经历，存入 fact_draft.education_json。"""

    education_id: UUID
    draft_status: ItemStatus
    school: str
    major: str | None = None
    degree: DegreeLevel
    status: EducationStatus | None = None
    start_text: str | None = None
    end_text: str | None = None
    source: FactSource
    source_doc_id: UUID | None = None
    source_quote: str | None = None
    span_start: int | None = None
    span_end: int | None = None
    quote_verified: bool = False
    review_reasons: list[ReviewReason] = Field(default_factory=list)

    @property
    def blocked(self) -> bool:
        """原文未通过校验或就读状态未明确时不能直接确认。"""
        return (
            self.source is FactSource.RESUME and not self.quote_verified
        ) or self.status is None

    @classmethod
    def from_extracted(cls, item: ExtractedEducationDraft) -> "DraftEducation":
        return cls(
            education_id=uuid4(),
            draft_status=ItemStatus.PENDING,
            source=FactSource.RESUME,
            **item.model_dump(),
        )

    @classmethod
    def from_published(
        cls, row: FactSetEducation, draft_status: ItemStatus
    ) -> "DraftEducation":
        return cls(
            education_id=row.education_id,
            draft_status=draft_status,
            school=row.school,
            major=row.major,
            degree=DegreeLevel(row.degree),
            status=EducationStatus(row.status),
            start_text=row.start_text,
            end_text=row.end_text,
            source=FactSource(row.source),
            source_doc_id=row.source_doc_id,
            source_quote=row.source_quote,
            span_start=row.span_start,
            span_end=row.span_end,
            quote_verified=row.quote_verified,
        )

    def to_row(self, fact_set_id: UUID, user_id: UUID) -> FactSetEducation:
        if self.status is None:
            raise CannotConfirmError(self.education_id)
        return FactSetEducation(
            fact_set_id=fact_set_id,
            user_id=user_id,
            education_id=self.education_id,
            school=self.school,
            major=self.major,
            degree=self.degree,
            status=self.status,
            start_text=self.start_text,
            end_text=self.end_text,
            source=self.source,
            source_doc_id=self.source_doc_id,
            source_quote=self.source_quote,
            span_start=self.span_start,
            span_end=self.span_end,
            quote_verified=self.quote_verified,
        )


class EducationChanges(BaseModel):
    """修改一段教育经历；只更新显式传入的字段。"""

    school: str | None = Field(default=None, min_length=1, max_length=200)
    major: str | None = Field(default=None, max_length=200)
    degree: DegreeLevel | None = None
    status: EducationStatus | None = None
    start_text: str | None = Field(default=None, max_length=60)
    end_text: str | None = Field(default=None, max_length=60)
    confirmed: bool | None = None


# ---------- 错误 ----------


class DraftError(Exception):
    """草稿操作被拒绝；接口层把各子类转成对应的 HTTP 状态码。"""


class ItemNotFoundError(DraftError):
    """草稿和最新版本里都没有这条事实，也可能是别人的。"""


class CannotConfirmError(DraftError):
    """原文校验没通过或信息不完整的条目不能确认。"""


class CriterionValueError(DraftError):
    """求职条件或教育字段取值不合法。"""


class VersionConflictError(DraftError):
    """发布时最新版本已经变了，例如另一个窗口先发布了。"""


class DraftRevisionConflictError(DraftError):
    """请求携带的草稿修订号已经过期。"""

    def __init__(self, revision: int) -> None:
        super().__init__(revision)
        self.revision = revision


class NothingToPublishError(DraftError):
    """草稿里没有已确认或待删除的条目。"""


# ---------- 读取 ----------


async def latest_fact_set(db: AsyncSession, user_id: UUID) -> FactSet | None:
    query = (
        select(FactSet)
        .where(FactSet.user_id == user_id)
        .order_by(FactSet.version.desc())
        .limit(1)
    )
    return await db.scalar(query)


async def facts_of(db: AsyncSession, fact_set: FactSet | None) -> list[Fact]:
    if fact_set is None:
        return []
    return await facts_in(db, fact_set.user_id, fact_set.fact_set_id)


async def facts_in(db: AsyncSession, user_id: UUID, fact_set_id: UUID) -> list[Fact]:
    """一个版本的事实，按简历本来的顺序。

    同一版本的事实在发布时的一个事务里写入，created_at 相同，只按 fact_id 排等于随机；
    所以再按出处和原文位置排，用户补充的（没有出处）排在后面。
    匹配和改写的提示词、改写计划都依赖这个顺序，顺序不稳定会让结果不可复现。
    """
    query = (
        select(Fact)
        .where(Fact.fact_set_id == fact_set_id, Fact.user_id == user_id)
        .order_by(
            Fact.created_at,
            Fact.source_doc_id.nulls_last(),
            Fact.span_start.nulls_last(),
            Fact.fact_id,
        )
    )
    return list((await db.scalars(query)).all())


async def read_draft(db: AsyncSession, user_id: UUID) -> FactDraft | None:
    """只读，不加锁；用于展示。"""
    return await db.get(FactDraft, user_id)


async def read_snapshot(
    db: AsyncSession, user_id: UUID
) -> tuple[FactDraft | None, FactSet | None]:
    """用同一条查询读草稿与最新版本，避免看到刚发布前的内容和发布后的修订号。"""
    query = (
        select(FactDraft, FactSet)
        .select_from(User)
        .outerjoin(FactDraft, FactDraft.user_id == User.user_id)
        .outerjoin(FactSet, FactSet.user_id == User.user_id)
        .where(User.user_id == user_id)
        .order_by(FactSet.version.desc())
        .limit(1)
    )
    row = (await db.execute(query)).one_or_none()
    return (row[0], row[1]) if row is not None else (None, None)


def revision_of(draft: FactDraft | None) -> int:
    return draft.revision if draft is not None else 0


async def read_revision(db: AsyncSession, user_id: UUID) -> int:
    """写接口在提交前读取本事务写入的修订号，此时仍持有行锁。"""
    return revision_of(await read_draft(db, user_id))


def items_of(draft: FactDraft | None) -> list[DraftItem]:
    return [
        DraftItem.model_validate(raw) for raw in (draft.items_json if draft else [])
    ]


def draft_criteria_of(draft: FactDraft | None) -> list[DraftCriterion]:
    return [
        DraftCriterion.model_validate(raw)
        for raw in (draft.criteria_json if draft else [])
    ]


def draft_educations_of(draft: FactDraft | None) -> list[DraftEducation]:
    return [
        DraftEducation.model_validate(raw)
        for raw in (draft.education_json if draft else [])
    ]


async def criteria_of(
    db: AsyncSession, fact_set: FactSet | None
) -> list[FactSetCriterion]:
    if fact_set is None:
        return []
    return await criteria_in(db, fact_set.user_id, fact_set.fact_set_id)


async def criteria_in(
    db: AsyncSession, user_id: UUID, fact_set_id: UUID
) -> list[FactSetCriterion]:
    query = (
        select(FactSetCriterion)
        .where(
            FactSetCriterion.fact_set_id == fact_set_id,
            FactSetCriterion.user_id == user_id,
        )
        .order_by(FactSetCriterion.field)
    )
    return list((await db.scalars(query)).all())


async def educations_of(
    db: AsyncSession, fact_set: FactSet | None
) -> list[FactSetEducation]:
    if fact_set is None:
        return []
    return await educations_in(db, fact_set.user_id, fact_set.fact_set_id)


async def educations_in(
    db: AsyncSession, user_id: UUID, fact_set_id: UUID
) -> list[FactSetEducation]:
    query = (
        select(FactSetEducation)
        .where(
            FactSetEducation.fact_set_id == fact_set_id,
            FactSetEducation.user_id == user_id,
        )
        .order_by(
            FactSetEducation.created_at,
            FactSetEducation.source_doc_id.nulls_last(),
            FactSetEducation.span_start.nulls_last(),
            FactSetEducation.record_id,
        )
    )
    return list((await db.scalars(query)).all())


async def lock_draft(db: AsyncSession, user_id: UUID) -> FactDraft:
    """取得并锁住草稿行；没有就先建一行，基准为当前最新版本。

    建行用 INSERT ... ON CONFLICT DO NOTHING：两个请求同时第一次修改时只会建一行。
    """
    latest = await latest_fact_set(db, user_id)
    await db.execute(
        insert(FactDraft)
        .values(
            user_id=user_id,
            base_fact_set_id=latest.fact_set_id if latest else None,
        )
        .on_conflict_do_nothing(index_elements=[FactDraft.user_id])
    )
    query = (
        select(FactDraft)
        .where(FactDraft.user_id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return (await db.scalars(query)).one()


def save_items(draft: FactDraft, items: list[DraftItem]) -> None:
    """在草稿行锁内保存事实条目并推进修订号。"""
    save_draft(draft, items=items)


def save_draft(
    draft: FactDraft,
    *,
    items: list[DraftItem] | None = None,
    criteria: list[DraftCriterion] | None = None,
    educations: list[DraftEducation] | None = None,
) -> None:
    """在草稿行锁内保存修改的草稿部分并统一推进修订号。"""
    if items is not None:
        draft.items_json = [item.model_dump(mode="json") for item in items]
    if criteria is not None:
        draft.criteria_json = [item.model_dump(mode="json") for item in criteria]
    if educations is not None:
        draft.education_json = [item.model_dump(mode="json") for item in educations]
    draft.revision += 1


def _check_revision(draft: FactDraft, expected_revision: int | None) -> None:
    if expected_revision is not None and draft.revision != expected_revision:
        raise DraftRevisionConflictError(draft.revision)


def _find(items: list[DraftItem], lineage_id: UUID) -> DraftItem | None:
    return next((item for item in items if item.lineage_id == lineage_id), None)


def _find_criterion(
    items: list[DraftCriterion], field: CriterionField
) -> DraftCriterion | None:
    return next((item for item in items if item.field is field), None)


def _find_education(
    items: list[DraftEducation], education_id: UUID
) -> DraftEducation | None:
    return next((item for item in items if item.education_id == education_id), None)


async def _find_published(
    db: AsyncSession, draft: FactDraft, lineage_id: UUID
) -> Fact | None:
    """在草稿的基准版本里按 lineage_id 找已发布的事实。"""
    if draft.base_fact_set_id is None:
        return None
    query = select(Fact).where(
        Fact.fact_set_id == draft.base_fact_set_id,
        Fact.user_id == draft.user_id,
        Fact.lineage_id == lineage_id,
    )
    return await db.scalar(query)


async def _find_published_criterion(
    db: AsyncSession, draft: FactDraft, field: CriterionField
) -> FactSetCriterion | None:
    if draft.base_fact_set_id is None:
        return None
    query = select(FactSetCriterion).where(
        FactSetCriterion.fact_set_id == draft.base_fact_set_id,
        FactSetCriterion.user_id == draft.user_id,
        FactSetCriterion.field == field,
    )
    return await db.scalar(query)


async def _find_published_education(
    db: AsyncSession, draft: FactDraft, education_id: UUID
) -> FactSetEducation | None:
    if draft.base_fact_set_id is None:
        return None
    query = select(FactSetEducation).where(
        FactSetEducation.fact_set_id == draft.base_fact_set_id,
        FactSetEducation.user_id == draft.user_id,
        FactSetEducation.education_id == education_id,
    )
    return await db.scalar(query)


def _validate_criterion_values(
    field: CriterionField,
    int_value: int | None,
    text_value: str | None,
    list_value: list[str] | None,
) -> tuple[int | None, str | None, list[str] | None]:
    if field is CriterionField.PREFERRED_CITIES:
        if int_value is not None or text_value is not None or list_value is None:
            raise CriterionValueError("意向城市只能填写城市列表")
        cities = list(dict.fromkeys(city.strip() for city in list_value))
        if not 1 <= len(cities) <= PREFERRED_CITY_COUNT or any(
            not 1 <= len(city) <= PREFERRED_CITY_CHARS for city in cities
        ):
            raise CriterionValueError(
                f"请填写 1 至 {PREFERRED_CITY_COUNT} 个城市，"
                f"每项不超过 {PREFERRED_CITY_CHARS} 字"
            )
        return None, None, cities
    if list_value is not None:
        raise CriterionValueError("只有意向城市可以填写列表")
    if field in (CriterionField.EARLIEST_START, CriterionField.TARGET_DIRECTION):
        if int_value is not None:
            raise CriterionValueError("最早到岗时间和目标方向只能填写文字")
        cleaned = (text_value or "").strip()
        limit = (
            TARGET_DIRECTION_CHARS if field is CriterionField.TARGET_DIRECTION else 200
        )
        if not cleaned or len(cleaned) > limit:
            raise CriterionValueError(f"该求职条件不能为空且不超过 {limit} 字")
        return None, cleaned, None
    if text_value is not None:
        raise CriterionValueError("到岗天数和实习月数只能填整数")
    low, high = (
        (ONSITE_DAYS_MIN, ONSITE_DAYS_MAX)
        if field is CriterionField.ONSITE_DAYS_PER_WEEK
        else (INTERNSHIP_MONTHS_MIN, INTERNSHIP_MONTHS_MAX)
    )
    if int_value is None or not (low <= int_value <= high):
        raise CriterionValueError(f"{field.value} 必须在 {low} 到 {high} 之间")
    return int_value, None, None


def _clean_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _merge_criteria(
    existing: list[DraftCriterion], incoming: Sequence[DraftCriterion]
) -> list[DraftCriterion]:
    """已确认或待删除的求职条件不覆盖；待核对的用新抽取替换。"""
    merged = list(existing)
    for item in incoming:
        index = next(
            (i for i, cur in enumerate(merged) if cur.field is item.field), None
        )
        if index is None:
            merged.append(item)
        elif merged[index].status is ItemStatus.PENDING:
            merged[index] = item
    return merged


def _merge_educations(
    existing: list[DraftEducation],
    incoming: Sequence[DraftEducation],
    published: Sequence[FactSetEducation],
) -> list[DraftEducation]:
    """按学校与学位对应教育经历：不覆盖已确认或待删除，复用已有 education_id。"""
    merged = list(existing)
    used_pub_ids = {item.education_id for item in merged}
    for item in incoming:
        key = (item.school.strip(), item.degree)
        index = next(
            (
                i
                for i, cur in enumerate(merged)
                if (cur.school.strip(), cur.degree) == key
            ),
            None,
        )
        if index is not None:
            if merged[index].draft_status is ItemStatus.PENDING:
                merged[index] = item.model_copy(
                    update={"education_id": merged[index].education_id}
                )
            continue
        matched_pub = next(
            (
                row
                for row in published
                if (row.school.strip(), DegreeLevel(row.degree)) == key
                and row.education_id not in used_pub_ids
            ),
            None,
        )
        if matched_pub is not None:
            used_pub_ids.add(matched_pub.education_id)
            merged.append(
                item.model_copy(update={"education_id": matched_pub.education_id})
            )
        else:
            merged.append(item)
    return merged


# ---------- 修改草稿 ----------


async def add_extracted(
    db: AsyncSession,
    user_id: UUID,
    facts: list[DraftFact],
    criteria: Sequence[DraftCriterion | ExtractedCriterionDraft] = (),
    educations: Sequence[DraftEducation | ExtractedEducationDraft] = (),
) -> list[DraftItem]:
    """把拆分结果加进草稿；求职条件和教育按合并规则保留已确认项。"""
    draft = await lock_draft(db, user_id)
    new_items = [DraftItem.from_extracted(fact) for fact in facts]
    criterion_items = [
        c if isinstance(c, DraftCriterion) else DraftCriterion.from_extracted(c)
        for c in criteria
    ]
    education_items = [
        e if isinstance(e, DraftEducation) else DraftEducation.from_extracted(e)
        for e in educations
    ]
    next_criteria = (
        _merge_criteria(draft_criteria_of(draft), criterion_items)
        if criterion_items
        else None
    )
    next_educations = None
    if education_items:
        published_edus = (
            await educations_in(db, user_id, draft.base_fact_set_id)
            if draft.base_fact_set_id is not None
            else []
        )
        next_educations = _merge_educations(
            draft_educations_of(draft), education_items, published_edus
        )
    save_draft(
        draft,
        items=items_of(draft) + new_items,
        criteria=next_criteria,
        educations=next_educations,
    )
    return new_items


async def add_user_fact(
    db: AsyncSession,
    user_id: UUID,
    *,
    experience: str,
    kind: FactKind,
    responsibility: Responsibility,
    text: str,
) -> DraftItem:
    """用户补充的事实就是用户自己的说法，直接算已确认。"""
    draft = await lock_draft(db, user_id)
    item = DraftItem(
        lineage_id=uuid4(),
        status=ItemStatus.CONFIRMED,
        experience=experience,
        kind=kind,
        responsibility=responsibility,
        text=text,
        has_number=contains_number(text),
        source=FactSource.USER,
    )
    save_items(draft, [*items_of(draft), item])
    return item


async def edit_fact(
    db: AsyncSession,
    user_id: UUID,
    lineage_id: UUID,
    changes: FactChanges,
    *,
    expected_revision: int | None = None,
) -> DraftItem:
    """修改或确认一条事实。改已发布的事实时，先把它复制进草稿。"""
    draft = await lock_draft(db, user_id)
    _check_revision(draft, expected_revision)
    items = items_of(draft)
    item = _find(items, lineage_id)
    if item is None:
        published = await _find_published(db, draft, lineage_id)
        if published is None:
            raise ItemNotFoundError(lineage_id)
        item = DraftItem.from_published(published, ItemStatus.PENDING)
        items.append(item)
    if item.status is ItemStatus.DELETED:
        item.status = ItemStatus.PENDING  # 修改一条待删除的事实，视为撤销删除

    _apply_changes(item, changes)
    save_items(draft, items)
    return item


def _apply_changes(item: DraftItem, changes: FactChanges) -> None:
    """改文字后原文出处失效；用户修改职责后去掉职责类核对原因。"""
    if changes.text is not None and changes.text != item.text:
        item.text = changes.text
        item.has_number = contains_number(changes.text)
        item.source = FactSource.USER
        item.source_doc_id = item.source_quote = None
        item.span_start = item.span_end = None
        item.quote_verified = False
        item.review_reasons = []
    if changes.kind is not None:
        item.kind = changes.kind
    if changes.responsibility is not None:
        item.responsibility = changes.responsibility
        item.review_reasons = [
            reason
            for reason in item.review_reasons
            if reason not in _RESPONSIBILITY_REASONS
        ]
    if changes.confirmed is not None:
        if changes.confirmed and item.blocked:
            raise CannotConfirmError(item.lineage_id)
        item.status = ItemStatus.CONFIRMED if changes.confirmed else ItemStatus.PENDING


async def delete_fact(
    db: AsyncSession,
    user_id: UUID,
    lineage_id: UUID,
    *,
    expected_revision: int | None = None,
) -> None:
    """草稿里的新事实直接去掉；已发布的事实标记为下一版移除。"""
    draft = await lock_draft(db, user_id)
    _check_revision(draft, expected_revision)
    items = items_of(draft)
    published = await _find_published(db, draft, lineage_id)
    item = _find(items, lineage_id)
    if item is None and published is None:
        raise ItemNotFoundError(lineage_id)

    remaining = [i for i in items if i.lineage_id != lineage_id]
    if published is not None:
        remaining.append(DraftItem.from_published(published, ItemStatus.DELETED))
    save_items(draft, remaining)


async def confirm_clean(
    db: AsyncSession, user_id: UUID, *, expected_revision: int | None = None
) -> int:
    """确认全部没有核对原因的待确认条目，返回确认了几条。带原因的必须逐条看过。"""
    draft = await lock_draft(db, user_id)
    _check_revision(draft, expected_revision)
    items = items_of(draft)
    count = 0
    for item in items:
        if item.status is ItemStatus.PENDING and not item.review_reasons:
            if item.blocked:  # 防御：没通过校验的条目一定带原因
                continue
            item.status = ItemStatus.CONFIRMED
            count += 1
    save_items(draft, items)
    return count


async def set_responsibility(
    db: AsyncSession,
    user_id: UUID,
    *,
    experience: str,
    responsibility: Responsibility,
    expected_revision: int | None = None,
) -> int:
    """回答「这段经历里没写职责的工作，是你负责的吗」，返回改了几条。

    只改该经历中职责未写明、还没确认的项目或实习条目，并去掉这项原因；
    不直接确认，没有别的原因的条目之后可以用 confirm_clean 一起确认。
    """
    draft = await lock_draft(db, user_id)
    _check_revision(draft, expected_revision)
    items = items_of(draft)
    count = 0
    for item in items:
        if (
            item.status is ItemStatus.PENDING
            and item.experience == experience
            and item.section in RESPONSIBILITY_SECTIONS
            and ReviewReason.RESPONSIBILITY_UNSTATED in item.review_reasons
        ):
            item.responsibility = responsibility
            item.review_reasons = [
                reason
                for reason in item.review_reasons
                if reason is not ReviewReason.RESPONSIBILITY_UNSTATED
            ]
            count += 1
    save_items(draft, items)
    return count


# ---------- 修改求职条件与教育草稿 ----------


async def edit_criterion(
    db: AsyncSession,
    user_id: UUID,
    field: CriterionField,
    *,
    int_value: int | None = None,
    text_value: str | None = None,
    list_value: list[str] | None = None,
    confirmed: bool | None = None,
    delete: bool = False,
    expected_revision: int | None = None,
) -> DraftCriterion | None:
    """新增、修改、确认或删除一项求职条件；只点确认不改值时保留简历出处。"""
    draft = await lock_draft(db, user_id)
    _check_revision(draft, expected_revision)
    items = draft_criteria_of(draft)
    existing = _find_criterion(items, field)
    published = await _find_published_criterion(db, draft, field)

    if delete:
        if existing is None and published is None:
            raise ItemNotFoundError(field)
        remaining = [item for item in items if item.field is not field]
        if published is None:
            save_draft(draft, criteria=remaining)
            return None
        deleted = DraftCriterion.from_published(published, ItemStatus.DELETED)
        save_draft(draft, criteria=[*remaining, deleted])
        return deleted

    values = (
        _validate_criterion_values(field, int_value, text_value, list_value)
        if any(value is not None for value in (int_value, text_value, list_value))
        else None
    )
    if existing is None and published is None:
        if values is None:
            raise ItemNotFoundError(field)
        created = DraftCriterion(
            field=field,
            status=ItemStatus.CONFIRMED if confirmed else ItemStatus.PENDING,
            int_value=values[0],
            text_value=values[1],
            list_value=values[2],
            source=FactSource.USER,
        )
        save_draft(draft, criteria=[*items, created])
        return created

    if existing is None:
        assert published is not None
        existing = DraftCriterion.from_published(published, ItemStatus.PENDING)
        items.append(existing)
    if existing.status is ItemStatus.DELETED:
        existing.status = ItemStatus.PENDING

    if values is not None and values != (
        existing.int_value,
        existing.text_value,
        existing.list_value,
    ):
        existing.int_value, existing.text_value, existing.list_value = values
        existing.source = FactSource.USER
        existing.source_doc_id = existing.source_quote = None
        existing.span_start = existing.span_end = None
        existing.quote_verified = False
        existing.review_reasons = []

    if confirmed is not None:
        if confirmed and existing.blocked:
            raise CannotConfirmError(field)
        existing.status = ItemStatus.CONFIRMED if confirmed else ItemStatus.PENDING

    save_draft(draft, criteria=items)
    return existing


async def add_user_education(
    db: AsyncSession,
    user_id: UUID,
    *,
    school: str,
    major: str | None,
    degree: DegreeLevel,
    status: EducationStatus,
    start_text: str | None,
    end_text: str | None,
    expected_revision: int | None = None,
) -> DraftEducation:
    """用户手动补充一段教育经历，直接算已确认。"""
    draft = await lock_draft(db, user_id)
    _check_revision(draft, expected_revision)
    cleaned_school = school.strip()
    if not cleaned_school:
        raise CriterionValueError("学校名称不能为空")
    item = DraftEducation(
        education_id=uuid4(),
        draft_status=ItemStatus.CONFIRMED,
        school=cleaned_school,
        major=_clean_optional_text(major),
        degree=degree,
        status=status,
        start_text=_clean_optional_text(start_text),
        end_text=_clean_optional_text(end_text),
        source=FactSource.USER,
    )
    save_draft(draft, educations=[*draft_educations_of(draft), item])
    return item


async def edit_education(
    db: AsyncSession,
    user_id: UUID,
    education_id: UUID,
    changes: EducationChanges,
    *,
    expected_revision: int | None = None,
) -> DraftEducation:
    """修改或确认一段教育经历；只点确认或补选就读状态时保留简历出处。"""
    draft = await lock_draft(db, user_id)
    _check_revision(draft, expected_revision)
    items = draft_educations_of(draft)
    item = _find_education(items, education_id)
    if item is None:
        published = await _find_published_education(db, draft, education_id)
        if published is None:
            raise ItemNotFoundError(education_id)
        item = DraftEducation.from_published(published, ItemStatus.PENDING)
        items.append(item)
    if item.draft_status is ItemStatus.DELETED:
        item.draft_status = ItemStatus.PENDING

    _apply_education_changes(item, changes)
    save_draft(draft, educations=items)
    return item


def _apply_education_changes(item: DraftEducation, changes: EducationChanges) -> None:
    fields = changes.model_fields_set
    next_school = (
        changes.school.strip()
        if "school" in fields and changes.school is not None
        else item.school
    )
    if not next_school:
        raise CriterionValueError("学校名称不能为空")
    next_major = (
        _clean_optional_text(changes.major) if "major" in fields else item.major
    )
    next_degree = (
        changes.degree
        if "degree" in fields and changes.degree is not None
        else item.degree
    )
    next_start = (
        _clean_optional_text(changes.start_text)
        if "start_text" in fields
        else item.start_text
    )
    next_end = (
        _clean_optional_text(changes.end_text)
        if "end_text" in fields
        else item.end_text
    )

    before = (item.school, item.major, item.degree, item.start_text, item.end_text)
    after = (next_school, next_major, next_degree, next_start, next_end)
    if after != before:
        item.school, item.major, item.degree = next_school, next_major, next_degree
        item.start_text, item.end_text = next_start, next_end
        item.source = FactSource.USER
        item.source_doc_id = item.source_quote = None
        item.span_start = item.span_end = None
        item.quote_verified = False
        item.review_reasons = [
            r
            for r in item.review_reasons
            if r is ReviewReason.EDUCATION_STATUS_UNCERTAIN
        ]

    if "status" in fields:
        item.status = changes.status
        if changes.status is not None:
            item.review_reasons = [
                r
                for r in item.review_reasons
                if r is not ReviewReason.EDUCATION_STATUS_UNCERTAIN
            ]
        else:
            item.draft_status = ItemStatus.PENDING
            if ReviewReason.EDUCATION_STATUS_UNCERTAIN not in item.review_reasons:
                item.review_reasons.append(ReviewReason.EDUCATION_STATUS_UNCERTAIN)

    if changes.confirmed is not None:
        if changes.confirmed and item.blocked:
            raise CannotConfirmError(item.education_id)
        item.draft_status = (
            ItemStatus.CONFIRMED if changes.confirmed else ItemStatus.PENDING
        )


async def delete_education(
    db: AsyncSession,
    user_id: UUID,
    education_id: UUID,
    *,
    expected_revision: int | None = None,
) -> None:
    """草稿里的新教育经历直接去掉；已发布的标记为下一版移除。"""
    draft = await lock_draft(db, user_id)
    _check_revision(draft, expected_revision)
    items = draft_educations_of(draft)
    published = await _find_published_education(db, draft, education_id)
    item = _find_education(items, education_id)
    if item is None and published is None:
        raise ItemNotFoundError(education_id)

    remaining = [i for i in items if i.education_id != education_id]
    if published is not None:
        remaining.append(DraftEducation.from_published(published, ItemStatus.DELETED))
    save_draft(draft, educations=remaining)


# ---------- 发布 ----------


async def _publish_facts(
    db: AsyncSession,
    latest: FactSet | None,
    new_set_id: UUID,
    user_id: UUID,
    items: list[DraftItem],
) -> list[DraftItem]:
    changed = {i.lineage_id: i for i in items if i.status != ItemStatus.PENDING}
    for fact in await facts_of(db, latest):
        if fact.lineage_id not in changed:
            db.add(
                DraftItem.from_published(fact, ItemStatus.CONFIRMED).to_fact(
                    new_set_id, user_id
                )
            )
    for item in changed.values():
        if item.status is ItemStatus.CONFIRMED:
            db.add(item.to_fact(new_set_id, user_id))
    return [i for i in items if i.status is ItemStatus.PENDING]


async def _publish_criteria(
    db: AsyncSession,
    latest: FactSet | None,
    new_set_id: UUID,
    user_id: UUID,
    items: list[DraftCriterion],
) -> list[DraftCriterion]:
    changed = {i.field: i for i in items if i.status != ItemStatus.PENDING}
    for row in await criteria_of(db, latest):
        if CriterionField(row.field) not in changed:
            db.add(
                DraftCriterion.from_published(row, ItemStatus.CONFIRMED).to_row(
                    new_set_id, user_id
                )
            )
    for item in changed.values():
        if item.status is ItemStatus.CONFIRMED:
            db.add(item.to_row(new_set_id, user_id))
    return [i for i in items if i.status is ItemStatus.PENDING]


async def _publish_educations(
    db: AsyncSession,
    latest: FactSet | None,
    new_set_id: UUID,
    user_id: UUID,
    items: list[DraftEducation],
) -> list[DraftEducation]:
    changed = {i.education_id: i for i in items if i.draft_status != ItemStatus.PENDING}
    for row in await educations_of(db, latest):
        if row.education_id not in changed:
            db.add(
                DraftEducation.from_published(row, ItemStatus.CONFIRMED).to_row(
                    new_set_id, user_id
                )
            )
    for item in changed.values():
        if item.draft_status is ItemStatus.CONFIRMED:
            db.add(item.to_row(new_set_id, user_id))
    return [i for i in items if i.draft_status is ItemStatus.PENDING]


async def publish_draft(
    db: AsyncSession,
    user_id: UUID,
    expected_base: UUID | None,
    *,
    expected_revision: int | None = None,
) -> FactSet:
    """把草稿发布成新版本，未提交；调用方提交后才生效。

    新版本涵盖事实、求职条件和教育三部分；只改了求职条件或教育也能发布。
    """
    draft = await lock_draft(db, user_id)
    _check_revision(draft, expected_revision)
    latest = await latest_fact_set(db, user_id)
    latest_id = latest.fact_set_id if latest else None
    if expected_base != latest_id or draft.base_fact_set_id != latest_id:
        raise VersionConflictError(expected_base)

    items = items_of(draft)
    criteria = draft_criteria_of(draft)
    educations = draft_educations_of(draft)
    has_changes = (
        any(i.status != ItemStatus.PENDING for i in items)
        or any(c.status != ItemStatus.PENDING for c in criteria)
        or any(e.draft_status != ItemStatus.PENDING for e in educations)
    )
    if not has_changes:
        raise NothingToPublishError()

    new_set = FactSet(user_id=user_id, version=latest.version + 1 if latest else 1)
    db.add(new_set)
    await db.flush()

    left_items = await _publish_facts(db, latest, new_set.fact_set_id, user_id, items)
    left_criteria = await _publish_criteria(
        db, latest, new_set.fact_set_id, user_id, criteria
    )
    left_educations = await _publish_educations(
        db, latest, new_set.fact_set_id, user_id, educations
    )

    draft.base_fact_set_id = new_set.fact_set_id
    save_draft(
        draft, items=left_items, criteria=left_criteria, educations=left_educations
    )
    await db.flush()
    return new_set
