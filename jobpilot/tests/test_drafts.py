"""草稿与发布：在真实测试库上验证 06 T1.4 第 ⑤ 步的验收清单，不调用模型。"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.db.models import (
    Fact,
    FactDraft,
    FactKind,
    FactSet,
    FactSource,
    Responsibility,
    ResumeDocument,
    SectionKind,
    User,
)
from jobpilot.services import drafts
from jobpilot.services.drafts import (
    CannotConfirmError,
    DraftItem,
    DraftRevisionConflictError,
    FactChanges,
    ItemNotFoundError,
    ItemStatus,
    NothingToPublishError,
    VersionConflictError,
)
from jobpilot.services.facts import DraftFact, ReviewReason

Factory = async_sessionmaker[AsyncSession]

RAW_TEXT = (
    "项目｜课程排班面板\n负责实现按周次筛选课程的功能\n优化查询后响应时间降低 30%\n"
)


@pytest.fixture
async def owner(session_factory: Factory) -> tuple[UUID, UUID]:
    """建一个用户和一份简历原文，返回 (user_id, doc_id)。"""
    async with session_factory.begin() as db:
        user = User(email="drafts@example.invalid", hashed_password="test")
        db.add(user)
        await db.flush()
        doc = ResumeDocument(user_id=user.user_id, raw_text=RAW_TEXT)
        db.add(doc)
        await db.flush()
        return user.user_id, doc.doc_id


def extracted(doc_id: UUID, quote: str, **overrides: Any) -> DraftFact:
    """一条拆分结果；默认原文校验通过、没有核对原因。"""
    start = RAW_TEXT.find(quote)
    verified = start >= 0
    values: dict[str, Any] = {
        "experience": "课程排班面板",
        "section": SectionKind.PROJECT,
        "kind": FactKind.ACTION,
        "responsibility": Responsibility.OWN,
        "text": quote,
        "has_number": False,
        "source_doc_id": doc_id,
        "source_quote": quote,
        "span_start": start if verified else None,
        "span_end": start + len(quote) if verified else None,
        "quote_verified": verified,
        "review_reasons": [] if verified else [ReviewReason.QUOTE_NOT_FOUND],
    }
    return DraftFact(**(values | overrides))


async def run(
    session_factory: Factory, step: Callable[[AsyncSession], Awaitable[Any]]
) -> Any:
    """在一个事务里执行一步并提交，模拟一次接口请求。"""
    async with session_factory.begin() as db:
        return await step(db)


async def publish(session_factory: Factory, user_id: UUID) -> FactSet:
    """以当前最新版本为基准发布，就像客户端刷新后点发布。"""

    async def step(db: AsyncSession) -> FactSet:
        latest = await drafts.latest_fact_set(db, user_id)
        base = latest.fact_set_id if latest else None
        return await drafts.publish_draft(db, user_id, base)

    return await run(session_factory, step)


async def facts_in(session_factory: Factory, fact_set: FactSet) -> list[Fact]:
    return await run(session_factory, lambda db: drafts.facts_of(db, fact_set))


async def draft_items(session_factory: Factory, user_id: UUID) -> list[DraftItem]:
    async def step(db: AsyncSession) -> list[DraftItem]:
        return drafts.items_of(await drafts.read_draft(db, user_id))

    return await run(session_factory, step)


async def add(session_factory: Factory, user_id: UUID, *facts: DraftFact) -> list:
    return await run(
        session_factory, lambda db: drafts.add_extracted(db, user_id, list(facts))
    )


async def edit(
    session_factory: Factory, user_id: UUID, lineage_id: UUID, **changes: Any
) -> DraftItem:
    return await run(
        session_factory,
        lambda db: drafts.edit_fact(db, user_id, lineage_id, FactChanges(**changes)),
    )


# ---------- 拆分结果进草稿 ----------


async def test_extracted_facts_wait_for_confirmation(
    session_factory: Factory, owner: tuple[UUID, UUID]
) -> None:
    user_id, doc_id = owner
    first, second = await add(
        session_factory,
        user_id,
        extracted(doc_id, "负责实现按周次筛选课程的功能"),
        extracted(doc_id, "优化查询后响应时间降低 30%"),
    )

    assert first.lineage_id != second.lineage_id
    items = await draft_items(session_factory, user_id)
    assert [item.status for item in items] == [ItemStatus.PENDING] * 2
    assert {item.source for item in items} == {FactSource.RESUME}


# ---------- 发布：只有确认过的进版本 ----------


async def test_only_confirmed_items_are_published(
    session_factory: Factory, owner: tuple[UUID, UUID]
) -> None:
    user_id, doc_id = owner
    confirmed, pending = await add(
        session_factory,
        user_id,
        extracted(doc_id, "负责实现按周次筛选课程的功能"),
        extracted(doc_id, "优化查询后响应时间降低 30%"),
    )
    await edit(session_factory, user_id, confirmed.lineage_id, confirmed=True)

    v1 = await publish(session_factory, user_id)

    assert v1.version == 1
    [fact] = await facts_in(session_factory, v1)
    assert fact.lineage_id == confirmed.lineage_id
    assert fact.quote_verified and fact.source == FactSource.RESUME
    [left] = await draft_items(session_factory, user_id)
    assert (left.lineage_id, left.status) == (pending.lineage_id, ItemStatus.PENDING)


async def test_item_failing_quote_check_cannot_be_confirmed(
    session_factory: Factory, owner: tuple[UUID, UUID]
) -> None:
    user_id, doc_id = owner
    [blocked] = await add(
        session_factory, user_id, extracted(doc_id, "原文里没有这一句")
    )

    with pytest.raises(CannotConfirmError):
        await edit(session_factory, user_id, blocked.lineage_id, confirmed=True)

    # 改写文字后转为用户补充，就可以确认了。
    item = await edit(
        session_factory,
        user_id,
        blocked.lineage_id,
        text="实现了课程筛选",
        confirmed=True,
    )
    assert item.source is FactSource.USER
    assert item.status is ItemStatus.CONFIRMED


async def test_nothing_to_publish(
    session_factory: Factory, owner: tuple[UUID, UUID]
) -> None:
    user_id, doc_id = owner
    await add(
        session_factory, user_id, extracted(doc_id, "负责实现按周次筛选课程的功能")
    )

    with pytest.raises(NothingToPublishError):
        await publish(session_factory, user_id)


# ---------- 修订：30% 改成 20% ----------


async def test_correction_appends_new_row_with_same_lineage(
    session_factory: Factory, owner: tuple[UUID, UUID]
) -> None:
    user_id, doc_id = owner
    keep, result = await add(
        session_factory,
        user_id,
        extracted(doc_id, "负责实现按周次筛选课程的功能"),
        extracted(
            doc_id, "优化查询后响应时间降低 30%", kind=FactKind.RESULT, has_number=True
        ),
    )
    for item in (keep, result):
        await edit(session_factory, user_id, item.lineage_id, confirmed=True)
    v1 = await publish(session_factory, user_id)

    corrected = await edit(
        session_factory,
        user_id,
        result.lineage_id,
        text="优化查询后响应时间降低 20%",
        confirmed=True,
    )
    v2 = await publish(session_factory, user_id)

    assert corrected.source is FactSource.USER
    old = {f.lineage_id: f for f in await facts_in(session_factory, v1)}
    new = {f.lineage_id: f for f in await facts_in(session_factory, v2)}
    assert old.keys() == new.keys() == {keep.lineage_id, result.lineage_id}
    # 旧版本不变：仍是原文来源的 30%
    assert old[result.lineage_id].text == "优化查询后响应时间降低 30%"
    assert old[result.lineage_id].source == FactSource.RESUME
    # 新版本：同一 lineage 的新一行，用户补充，不带出处
    revised = new[result.lineage_id]
    assert revised.fact_id != old[result.lineage_id].fact_id
    assert (revised.text, revised.source, revised.source_quote) == (
        "优化查询后响应时间降低 20%",
        FactSource.USER,
        None,
    )
    # 没动过的事实复制到新版本：同一 lineage，新的一行，内容不变
    assert new[keep.lineage_id].fact_id != old[keep.lineage_id].fact_id
    assert new[keep.lineage_id].source_quote == old[keep.lineage_id].source_quote
    # 栏目随事实发布，复制到新版本、改了文字后都保留，改写时按它分块。
    assert {fact.section for fact in [*old.values(), *new.values()]} == {"project"}


async def test_changing_responsibility_keeps_resume_source(
    session_factory: Factory, owner: tuple[UUID, UUID]
) -> None:
    user_id, doc_id = owner
    [item] = await add(
        session_factory,
        user_id,
        extracted(
            doc_id,
            "负责实现按周次筛选课程的功能",
            responsibility=Responsibility.PARTICIPATE,
            review_reasons=[ReviewReason.RESPONSIBILITY_UNSTATED],
        ),
    )

    edited = await edit(
        session_factory, user_id, item.lineage_id, responsibility=Responsibility.OWN
    )

    assert edited.source is FactSource.RESUME and edited.quote_verified
    assert edited.review_reasons == []


# ---------- 删除 ----------


async def test_delete_published_fact_removes_it_from_next_version_only(
    session_factory: Factory, owner: tuple[UUID, UUID]
) -> None:
    user_id, doc_id = owner
    [item] = await add(
        session_factory, user_id, extracted(doc_id, "负责实现按周次筛选课程的功能")
    )
    await edit(session_factory, user_id, item.lineage_id, confirmed=True)
    v1 = await publish(session_factory, user_id)

    await run(
        session_factory, lambda db: drafts.delete_fact(db, user_id, item.lineage_id)
    )
    v2 = await publish(session_factory, user_id)

    assert len(await facts_in(session_factory, v1)) == 1
    assert await facts_in(session_factory, v2) == []


async def test_delete_unpublished_item_and_unknown_lineage(
    session_factory: Factory, owner: tuple[UUID, UUID]
) -> None:
    user_id, doc_id = owner
    [item] = await add(
        session_factory, user_id, extracted(doc_id, "负责实现按周次筛选课程的功能")
    )

    await run(
        session_factory, lambda db: drafts.delete_fact(db, user_id, item.lineage_id)
    )

    assert await draft_items(session_factory, user_id) == []
    with pytest.raises(ItemNotFoundError):
        await run(
            session_factory, lambda db: drafts.delete_fact(db, user_id, item.lineage_id)
        )


# ---------- 批量操作 ----------


async def test_confirm_clean_skips_items_with_reasons(
    session_factory: Factory, owner: tuple[UUID, UUID]
) -> None:
    user_id, doc_id = owner
    clean, flagged, blocked = await add(
        session_factory,
        user_id,
        extracted(doc_id, "负责实现按周次筛选课程的功能"),
        extracted(
            doc_id,
            "优化查询后响应时间降低 30%",
            review_reasons=[ReviewReason.RESPONSIBILITY_UNSTATED],
        ),
        extracted(doc_id, "原文里没有这一句"),
    )

    count = await run(session_factory, lambda db: drafts.confirm_clean(db, user_id))

    assert count == 1
    status = {
        i.lineage_id: i.status for i in await draft_items(session_factory, user_id)
    }
    assert status == {
        clean.lineage_id: ItemStatus.CONFIRMED,
        flagged.lineage_id: ItemStatus.PENDING,
        blocked.lineage_id: ItemStatus.PENDING,
    }


async def test_set_responsibility_only_touches_unstated_items_of_that_experience(
    session_factory: Factory, owner: tuple[UUID, UUID]
) -> None:
    user_id, doc_id = owner
    unstated = [ReviewReason.RESPONSIBILITY_UNSTATED]
    target, other_experience, stated, skill = await add(
        session_factory,
        user_id,
        extracted(
            doc_id,
            "负责实现按周次筛选课程的功能",
            responsibility=Responsibility.PARTICIPATE,
            review_reasons=unstated,
        ),
        extracted(
            doc_id,
            "优化查询后响应时间降低 30%",
            experience="另一段经历",
            responsibility=Responsibility.PARTICIPATE,
            review_reasons=unstated,
        ),
        extracted(doc_id, "项目｜课程排班面板"),  # 职责写明了
        extracted(
            doc_id,
            "负责实现按周次筛选课程的功能",
            section=SectionKind.SKILL,
            responsibility=Responsibility.NA,
            review_reasons=unstated,
        ),
    )

    count = await run(
        session_factory,
        lambda db: drafts.set_responsibility(
            db,
            user_id,
            experience="课程排班面板",
            responsibility=Responsibility.OWN,
        ),
    )

    assert count == 1
    items = {i.lineage_id: i for i in await draft_items(session_factory, user_id)}
    assert items[target.lineage_id].responsibility is Responsibility.OWN
    assert items[target.lineage_id].review_reasons == []
    assert items[target.lineage_id].status is ItemStatus.PENDING  # 不直接确认
    for untouched in (other_experience, stated, skill):
        assert items[untouched.lineage_id] == untouched


# ---------- 并发与原子性 ----------


async def test_stale_base_is_rejected(
    session_factory: Factory, owner: tuple[UUID, UUID]
) -> None:
    user_id, doc_id = owner
    [item] = await add(
        session_factory, user_id, extracted(doc_id, "负责实现按周次筛选课程的功能")
    )
    await edit(session_factory, user_id, item.lineage_id, confirmed=True)

    with pytest.raises(VersionConflictError):
        await run(
            session_factory,
            lambda db: drafts.publish_draft(db, user_id, UUID(int=1)),
        )


async def test_concurrent_publish_waits_for_the_first_then_conflicts(
    session_factory: Factory, owner: tuple[UUID, UUID]
) -> None:
    """两个窗口都基于「还没有版本」点发布。第一个发布后先不提交：
    第二个必须在草稿锁上等待，第一个提交后它看到版本已变，返回冲突。"""
    user_id, doc_id = owner
    [item] = await add(
        session_factory, user_id, extracted(doc_id, "负责实现按周次筛选课程的功能")
    )
    await edit(session_factory, user_id, item.lineage_id, confirmed=True)

    async with session_factory() as first:
        await drafts.publish_draft(first, user_id, None)
        second = asyncio.create_task(
            run(session_factory, lambda db: drafts.publish_draft(db, user_id, None))
        )
        await asyncio.sleep(0.3)
        assert not second.done()  # 在等第一个事务释放草稿锁
        await first.commit()

    with pytest.raises(VersionConflictError):
        await second
    async with session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(FactSet)) == 1


async def test_concurrent_edits_do_not_overwrite_each_other(
    session_factory: Factory, owner: tuple[UUID, UUID]
) -> None:
    """两个请求都读了草稿再写，后写的会覆盖先写的。草稿锁让第二个请求等第一个提交。"""
    user_id, doc_id = owner
    first_item, second_item = await add(
        session_factory,
        user_id,
        extracted(doc_id, "负责实现按周次筛选课程的功能"),
        extracted(doc_id, "优化查询后响应时间降低 30%"),
    )

    async with session_factory() as first:
        draft = await drafts.lock_draft(first, user_id)  # 读了草稿，还没写
        second = asyncio.create_task(
            edit(session_factory, user_id, second_item.lineage_id, confirmed=True)
        )
        await asyncio.sleep(0.3)
        assert not second.done()  # 第二个请求在等草稿锁
        items = drafts.items_of(draft)
        items[0].status = ItemStatus.CONFIRMED
        drafts.save_items(draft, items)
        await first.commit()
    await second

    statuses = {
        i.lineage_id: i.status for i in await draft_items(session_factory, user_id)
    }
    assert statuses == {
        first_item.lineage_id: ItemStatus.CONFIRMED,
        second_item.lineage_id: ItemStatus.CONFIRMED,
    }


async def test_concurrent_confirmation_checks_revision_after_waiting_for_edit(
    session_factory: Factory, owner: tuple[UUID, UUID]
) -> None:
    user_id, doc_id = owner
    [item] = await add(
        session_factory, user_id, extracted(doc_id, "优化查询后响应时间降低 30%")
    )
    new_text = "优化查询后响应时间降低 20%"
    async with session_factory() as first:
        revision = await drafts.read_revision(first, user_id)
        await drafts.edit_fact(
            first, user_id, item.lineage_id, FactChanges(text=new_text)
        )
        confirmation = asyncio.create_task(
            run(
                session_factory,
                lambda db: drafts.edit_fact(
                    db,
                    user_id,
                    item.lineage_id,
                    FactChanges(confirmed=True),
                    expected_revision=revision,
                ),
            )
        )
        await asyncio.sleep(0.3)
        assert not confirmation.done()
        await first.commit()

    with pytest.raises(DraftRevisionConflictError) as error:
        await confirmation
    assert error.value.revision == revision + 1
    [current] = await draft_items(session_factory, user_id)
    assert current.text == new_text
    assert current.status is ItemStatus.PENDING


async def test_failed_publish_leaves_no_partial_version(
    session_factory: Factory, owner: tuple[UUID, UUID]
) -> None:
    """发布到一半违反约束：版本和事实都不写入，草稿保持原样。"""
    user_id, doc_id = owner
    good, bad = await add(
        session_factory,
        user_id,
        extracted(doc_id, "负责实现按周次筛选课程的功能"),
        extracted(doc_id, "优化查询后响应时间降低 30%"),
    )
    await edit(session_factory, user_id, good.lineage_id, confirmed=True)

    # 绕过接口直接改草稿：一条没通过校验的简历事实被标成已确认，数据库 CHECK 会拒绝。
    async with session_factory.begin() as db:
        draft = await db.get(FactDraft, user_id)
        assert draft is not None
        items = drafts.items_of(draft)
        broken = next(i for i in items if i.lineage_id == bad.lineage_id)
        broken.status, broken.quote_verified = ItemStatus.CONFIRMED, False
        drafts.save_items(draft, items)
    before = await draft_items(session_factory, user_id)

    with pytest.raises(IntegrityError):
        await publish(session_factory, user_id)

    async with session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(FactSet)) == 0
        assert await db.scalar(select(func.count()).select_from(Fact)) == 0
    assert await draft_items(session_factory, user_id) == before
