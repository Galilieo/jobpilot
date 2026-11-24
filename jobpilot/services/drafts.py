"""事实库草稿与发布：确认、修改、补充、删除都先进草稿，发布时生成新版本。

每个用户一份草稿（fact_draft 一行），条目放在 items_json。
条目用 lineage_id 指代一条事实：已发布的事实沿用原有的 lineage_id，
草稿里的新事实在创建时分配。

这里的函数只改 session 里的对象，不提交；调用方在同一个事务里提交。
修改草稿前都先锁住草稿行（SELECT ... FOR UPDATE），同一用户的并发修改排队执行。
"""

from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.db.models import (
    Fact,
    FactDraft,
    FactKind,
    FactSet,
    FactSource,
    Responsibility,
    SectionKind,
    User,
)
from jobpilot.services.facts import (
    RESPONSIBILITY_SECTIONS,
    DraftFact,
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


# ---------- 错误 ----------


class DraftError(Exception):
    """草稿操作被拒绝；接口层把各子类转成对应的 HTTP 状态码。"""


class ItemNotFoundError(DraftError):
    """草稿和最新版本里都没有这条事实，也可能是别人的。"""


class CannotConfirmError(DraftError):
    """原文校验没通过的条目不能确认。"""


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
    """在草稿行锁内保存条目并推进修订号。"""
    draft.items_json = [item.model_dump(mode="json") for item in items]
    draft.revision += 1


def _check_revision(draft: FactDraft, expected_revision: int | None) -> None:
    if expected_revision is not None and draft.revision != expected_revision:
        raise DraftRevisionConflictError(draft.revision)


def _find(items: list[DraftItem], lineage_id: UUID) -> DraftItem | None:
    return next((item for item in items if item.lineage_id == lineage_id), None)


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


# ---------- 修改草稿 ----------


async def add_extracted(
    db: AsyncSession, user_id: UUID, facts: list[DraftFact]
) -> list[DraftItem]:
    """把拆分结果加进草稿，全部待确认。"""
    draft = await lock_draft(db, user_id)
    new_items = [DraftItem.from_extracted(fact) for fact in facts]
    save_items(draft, items_of(draft) + new_items)
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


async def delete_fact(db: AsyncSession, user_id: UUID, lineage_id: UUID) -> None:
    """草稿里的新事实直接去掉；已发布的事实标记为下一版移除。"""
    draft = await lock_draft(db, user_id)
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


# ---------- 发布 ----------


async def publish_draft(
    db: AsyncSession,
    user_id: UUID,
    expected_base: UUID | None,
    *,
    expected_revision: int | None = None,
) -> FactSet:
    """把草稿发布成新版本，未提交；调用方提交后才生效。

    expected_base 是客户端开始编辑时看到的最新版本，第一次发布时为 None。
    新版本 = 上一版里草稿没动过的事实 + 已确认的条目；待删除的不再出现；
    待确认的留在草稿里。版本已变时抛 VersionConflictError，没有改动时抛
    NothingToPublishError。
    """
    draft = await lock_draft(db, user_id)
    _check_revision(draft, expected_revision)
    latest = await latest_fact_set(db, user_id)
    latest_id = latest.fact_set_id if latest else None
    if expected_base != latest_id or draft.base_fact_set_id != latest_id:
        raise VersionConflictError(expected_base)

    items = items_of(draft)
    changed = {
        item.lineage_id: item for item in items if item.status != ItemStatus.PENDING
    }
    if not changed:
        raise NothingToPublishError()

    new_set = FactSet(user_id=user_id, version=latest.version + 1 if latest else 1)
    db.add(new_set)
    await db.flush()

    for fact in await facts_of(db, latest):
        if fact.lineage_id not in changed:
            carried = DraftItem.from_published(fact, ItemStatus.CONFIRMED)
            db.add(carried.to_fact(new_set.fact_set_id, user_id))
    for item in changed.values():
        if item.status is ItemStatus.CONFIRMED:
            db.add(item.to_fact(new_set.fact_set_id, user_id))

    draft.base_fact_set_id = new_set.fact_set_id
    save_items(draft, [item for item in items if item.status is ItemStatus.PENDING])
    await db.flush()
    return new_set
