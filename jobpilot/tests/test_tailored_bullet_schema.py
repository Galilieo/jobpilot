"""用真实 PostgreSQL 验证改写条目的组合外键、取值检查与级联删除。

要守住的是：条目只能挂在本人、运行启动时固定的那个事实库版本上；
条目引用的事实必须是同一用户、同一版本里的行。
"""

from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.db.models import AiRun, BulletFact, Fact, TailoredBullet, User
from jobpilot.tests.test_fact_schema import (
    add_document,
    add_fact_set,
    add_user,
    resume_fact,
    violated_constraint,
)

Factory = async_sessionmaker[AsyncSession]


async def add_run(
    session_factory: Factory, user_id: UUID, fact_set_id: UUID, doc_id: UUID | None
) -> UUID:
    async with session_factory.begin() as session:
        run = AiRun(
            user_id=user_id, kind="tailor", fact_set_id=fact_set_id, base_doc_id=doc_id
        )
        session.add(run)
        await session.flush()
        return run.run_id


async def add_fact(session_factory: Factory, fact: Fact) -> UUID:
    async with session_factory.begin() as session:
        session.add(fact)
        await session.flush()
        return fact.fact_id


def bullet(
    user_id: UUID, run_id: UUID, fact_set_id: UUID, **overrides
) -> TailoredBullet:
    values = {
        "user_id": user_id,
        "run_id": run_id,
        "fact_set_id": fact_set_id,
        "section": "experience",
        "experience": "课程排班面板",
        "order_no": 0,
        "text": "实现按周次筛选课程的功能",
        "check_status": "passed",
    } | overrides
    return TailoredBullet(**values)


async def add_bullet(session_factory: Factory, row: TailoredBullet) -> UUID:
    async with session_factory.begin() as session:
        session.add(row)
        await session.flush()
        return row.bullet_id


async def setup(session_factory: Factory, email: str):
    user = await add_user(session_factory, email)
    doc = await add_document(session_factory, user)
    v1 = await add_fact_set(session_factory, user, 1)
    v2 = await add_fact_set(session_factory, user, 2)
    fact_v1 = await add_fact(session_factory, resume_fact(user, v1, doc))
    run = await add_run(session_factory, user, v1, doc)
    return user, doc, v1, v2, fact_v1, run


async def test_bullet_must_match_run_user_and_fact_set(
    session_factory: Factory,
) -> None:
    a, _, a_v1, a_v2, _, a_run = await setup(session_factory, "a@bullet.invalid")
    b, _, b_v1, _, _, _ = await setup(session_factory, "b@bullet.invalid")

    # 同一用户，但条目写的版本不是运行固定的版本
    wrong_version = bullet(a, a_run, a_v2)
    # 别人的运行
    other_user = bullet(b, a_run, b_v1)
    for row in (wrong_version, other_user):
        assert await violated_constraint(session_factory, row) == (
            "fk_tailored_bullet_run_id_ai_run"
        )
    assert await add_bullet(session_factory, bullet(a, a_run, a_v1))


async def test_bullet_fact_must_be_same_user_and_version(
    session_factory: Factory,
) -> None:
    a, doc_a, a_v1, a_v2, fact_v1, a_run = await setup(
        session_factory, "c@bullet.invalid"
    )
    b, doc_b, b_v1, _, fact_b, _ = await setup(session_factory, "d@bullet.invalid")
    fact_v2 = await add_fact(session_factory, resume_fact(a, a_v2, doc_a))
    bullet_id = await add_bullet(session_factory, bullet(a, a_run, a_v1))

    def link(fact_id: UUID, user_id: UUID, fact_set_id: UUID) -> BulletFact:
        return BulletFact(
            bullet_id=bullet_id,
            fact_id=fact_id,
            user_id=user_id,
            fact_set_id=fact_set_id,
        )

    # 同一用户的新版本事实：版本对不上
    assert await violated_constraint(session_factory, link(fact_v2, a, a_v1)) == (
        "fk_bullet_fact_fact_id_fact"
    )
    # 冒充成条目的版本也不行：条目那一侧对不上
    assert await violated_constraint(session_factory, link(fact_v2, a, a_v2)) == (
        "fk_bullet_fact_bullet_id_tailored_bullet"
    )
    # 别人的事实
    assert await violated_constraint(session_factory, link(fact_b, a, a_v1)) == (
        "fk_bullet_fact_fact_id_fact"
    )
    async with session_factory.begin() as session:
        session.add(link(fact_v1, a, a_v1))


async def test_value_checks(session_factory: Factory) -> None:
    a, _, a_v1, _, _, a_run = await setup(session_factory, "e@bullet.invalid")
    cases = {
        "ck_tailored_bullet_section": {"section": "summary", "experience": None},
        "ck_tailored_bullet_experience": {"section": "skills"},  # 技能区不该有经历名
        "ck_tailored_bullet_check_status": {"check_status": "ok"},
        "ck_tailored_bullet_user_decision": {"user_decision": "maybe"},
        "ck_tailored_bullet_order_no": {"order_no": -1},
        "ck_tailored_bullet_revision_count": {"revision_count": 3},
    }
    for name, overrides in cases.items():
        row = bullet(a, a_run, a_v1, **overrides)
        assert await violated_constraint(session_factory, row) == name
    # 经历条目必须有经历名
    row = bullet(a, a_run, a_v1, experience=None)
    assert await violated_constraint(session_factory, row) == (
        "ck_tailored_bullet_experience"
    )
    # 同一次运行里顺序号不能重复
    await add_bullet(session_factory, bullet(a, a_run, a_v1, order_no=5))
    duplicate = bullet(a, a_run, a_v1, order_no=5)
    assert await violated_constraint(session_factory, duplicate) == (
        "uq_tailored_bullet_run_id"
    )
    # 技能区没有经历名是合法的
    assert await add_bullet(
        session_factory, bullet(a, a_run, a_v1, section="skills", experience=None)
    )


async def test_fact_section_check(session_factory: Factory) -> None:
    a, doc, a_v1, *_ = await setup(session_factory, "f@bullet.invalid")
    bad = resume_fact(a, a_v1, doc, section="hobby")
    assert await violated_constraint(session_factory, bad) == "ck_fact_section"
    assert await add_fact(session_factory, resume_fact(a, a_v1, doc, section="skill"))


async def test_run_base_doc_must_belong_to_same_user(session_factory: Factory) -> None:
    a, _, a_v1, *_ = await setup(session_factory, "g@bullet.invalid")
    _, doc_b, *_ = await setup(session_factory, "h@bullet.invalid")
    row = AiRun(user_id=a, kind="tailor", fact_set_id=a_v1, base_doc_id=doc_b)
    assert await violated_constraint(session_factory, row) == (
        "fk_ai_run_base_doc_id_resume_document"
    )


async def test_stop_reason_only_on_finished_runs(session_factory: Factory) -> None:
    user = await add_user(session_factory, "stop@bullet.invalid")
    running = AiRun(user_id=user, kind="tailor", stop_reason="budget_exhausted")
    assert (
        await violated_constraint(session_factory, running) == "ck_ai_run_stop_reason"
    )
    unknown = AiRun(
        user_id=user,
        kind="tailor",
        status="failed",
        finished_at=func.now(),
        stop_reason="tired",
    )
    assert (
        await violated_constraint(session_factory, unknown) == "ck_ai_run_stop_reason"
    )
    async with session_factory.begin() as session:
        session.add(
            AiRun(
                user_id=user,
                kind="tailor",
                status="budget_exhausted",
                finished_at=func.now(),
                stop_reason="budget_exhausted",
                plan_json={"blocks": []},
            )
        )


async def test_deleting_run_or_account_cascades(session_factory: Factory) -> None:
    a, _, a_v1, _, fact_v1, a_run = await setup(session_factory, "i@bullet.invalid")
    bullet_id = await add_bullet(session_factory, bullet(a, a_run, a_v1))
    async with session_factory.begin() as session:
        session.add(
            BulletFact(
                bullet_id=bullet_id, fact_id=fact_v1, user_id=a, fact_set_id=a_v1
            )
        )

    async with session_factory.begin() as session:
        await session.execute(delete(AiRun).where(AiRun.run_id == a_run))
    async with session_factory() as session:
        assert (
            await session.scalar(select(func.count()).select_from(TailoredBullet)) == 0
        )
        assert await session.scalar(select(func.count()).select_from(BulletFact)) == 0

    run2 = await add_run(session_factory, a, a_v1, None)
    bullet2 = await add_bullet(session_factory, bullet(a, run2, a_v1))
    async with session_factory.begin() as session:
        session.add(
            BulletFact(bullet_id=bullet2, fact_id=fact_v1, user_id=a, fact_set_id=a_v1)
        )
    async with session_factory.begin() as session:
        await session.execute(delete(User).where(User.user_id == a))
    async with session_factory() as session:
        assert (
            await session.scalar(select(func.count()).select_from(TailoredBullet)) == 0
        )
        assert await session.scalar(select(func.count()).select_from(BulletFact)) == 0
