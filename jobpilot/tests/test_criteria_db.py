"""用真实 PostgreSQL 验证求职条件与教育表的约束、只追加触发器与迁移往返。"""

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import delete, inspect, select, text, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alembic import command
from jobpilot.db.models import (
    AiRun,
    FactDraft,
    FactSetCriterion,
    FactSetEducation,
    MatchReport,
    ResumeVersion,
    TailoredResume,
    User,
)
from jobpilot.tests.test_fact_schema import (
    QUOTE,
    SPAN_START,
    add_document,
    add_fact_set,
    add_user,
    resume_fact,
    violated_constraint,
)

Factory = async_sessionmaker[AsyncSession]


def _criterion(
    user_id: UUID, fact_set_id: UUID, doc_id: UUID | None = None, **overrides: Any
) -> FactSetCriterion:
    values: dict[str, Any] = {
        "user_id": user_id,
        "fact_set_id": fact_set_id,
        "field": "onsite_days_per_week",
        "int_value": 4,
        "text_value": None,
        "source": "resume" if doc_id else "user",
        "source_doc_id": doc_id,
        "source_quote": QUOTE if doc_id else None,
        "span_start": SPAN_START if doc_id else None,
        "span_end": SPAN_START + len(QUOTE) if doc_id else None,
        "quote_verified": bool(doc_id),
    } | overrides
    return FactSetCriterion(**values)


def _education(
    user_id: UUID, fact_set_id: UUID, doc_id: UUID | None = None, **overrides: Any
) -> FactSetEducation:
    values: dict[str, Any] = {
        "user_id": user_id,
        "fact_set_id": fact_set_id,
        "education_id": uuid4(),
        "school": "东澜理工学院",
        "major": "软件工程",
        "degree": "bachelor",
        "status": "enrolled",
        "start_text": "2023.09",
        "end_text": "2027.06（预计）",
        "source": "resume" if doc_id else "user",
        "source_doc_id": doc_id,
        "source_quote": QUOTE if doc_id else None,
        "span_start": SPAN_START if doc_id else None,
        "span_end": SPAN_START + len(QUOTE) if doc_id else None,
        "quote_verified": bool(doc_id),
    } | overrides
    return FactSetEducation(**values)


async def test_cross_user_references_rejected_for_criteria_and_education(
    session_factory: Factory,
) -> None:
    user_a = await add_user(session_factory, "ca@criteria.invalid")
    user_b = await add_user(session_factory, "cb@criteria.invalid")
    doc_a = await add_document(session_factory, user_a)
    doc_b = await add_document(session_factory, user_b)
    set_a = await add_fact_set(session_factory, user_a, 1)
    set_b = await add_fact_set(session_factory, user_b, 1)

    cases = [
        (
            _criterion(user_a, set_b, doc_a),
            "fk_fact_set_criterion_fact_set_id_fact_set",
        ),
        (
            _criterion(user_a, set_a, doc_b),
            "fk_fact_set_criterion_source_doc_id_resume_document",
        ),
        (
            _education(user_a, set_b, doc_a),
            "fk_fact_set_education_fact_set_id_fact_set",
        ),
        (
            _education(user_a, set_a, doc_b),
            "fk_fact_set_education_source_doc_id_resume_document",
        ),
    ]
    for row, constraint in cases:
        assert await violated_constraint(session_factory, row) == constraint


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"field": "salary"}, "ck_fact_set_criterion_field"),
        ({"int_value": 0}, "ck_fact_set_criterion_value"),
        ({"int_value": 8}, "ck_fact_set_criterion_value"),
        ({"int_value": 4, "text_value": "4 天"}, "ck_fact_set_criterion_value"),
        (
            {"field": "internship_months", "int_value": 0},
            "ck_fact_set_criterion_value",
        ),
        (
            {"field": "internship_months", "int_value": 25},
            "ck_fact_set_criterion_value",
        ),
        (
            {"field": "earliest_start", "int_value": 1, "text_value": "立即"},
            "ck_fact_set_criterion_value",
        ),
        (
            {"field": "earliest_start", "int_value": None, "text_value": "   "},
            "ck_fact_set_criterion_value",
        ),
        ({"quote_verified": False}, "ck_fact_set_criterion_source"),
        ({"source": "user"}, "ck_fact_set_criterion_source"),
        ({"span_end": SPAN_START}, "ck_fact_set_criterion_span"),
    ],
)
async def test_criterion_value_and_source_checks(
    session_factory: Factory, overrides: dict[str, Any], constraint: str
) -> None:
    user = await add_user(session_factory, "crit-check@criteria.invalid")
    doc = await add_document(session_factory, user)
    fact_set = await add_fact_set(session_factory, user, 1)

    row = _criterion(user, fact_set, doc, **overrides)
    assert await violated_constraint(session_factory, row) == constraint


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"school": "   "}, "ck_fact_set_education_school"),
        ({"degree": "highschool"}, "ck_fact_set_education_degree"),
        ({"status": "dropped"}, "ck_fact_set_education_status"),
        ({"quote_verified": False}, "ck_fact_set_education_source"),
        ({"source": "user"}, "ck_fact_set_education_source"),
        ({"span_end": SPAN_START}, "ck_fact_set_education_span"),
    ],
)
async def test_education_value_and_source_checks(
    session_factory: Factory, overrides: dict[str, Any], constraint: str
) -> None:
    user = await add_user(session_factory, "edu-check@criteria.invalid")
    doc = await add_document(session_factory, user)
    fact_set = await add_fact_set(session_factory, user, 1)

    row = _education(user, fact_set, doc, **overrides)
    assert await violated_constraint(session_factory, row) == constraint


async def test_criterion_and_education_uniqueness_and_append_only(
    session_factory: Factory,
) -> None:
    user = await add_user(session_factory, "append@criteria.invalid")
    doc = await add_document(session_factory, user)
    fact_set = await add_fact_set(session_factory, user, 1)
    edu_id = uuid4()

    async with session_factory.begin() as session:
        session.add(_criterion(user, fact_set, doc))
        session.add(_education(user, fact_set, doc, education_id=edu_id))

    assert (
        await violated_constraint(session_factory, _criterion(user, fact_set, doc))
        == "uq_fact_set_criterion_fact_set_id"
    )
    assert (
        await violated_constraint(
            session_factory, _education(user, fact_set, doc, education_id=edu_id)
        )
        == "uq_fact_set_education_user_id"
    )

    with pytest.raises(DBAPIError, match="append-only"):
        async with session_factory.begin() as session:
            await session.execute(update(FactSetCriterion).values(int_value=5))

    with pytest.raises(DBAPIError, match="append-only"):
        async with session_factory.begin() as session:
            await session.execute(update(FactSetEducation).values(school="别的学校"))

    async with session_factory.begin() as session:
        await session.execute(delete(User).where(User.user_id == user))
    async with session_factory() as reader:
        assert (await reader.scalars(select(FactSetCriterion))).all() == []
        assert (await reader.scalars(select(FactSetEducation))).all() == []


async def test_criteria_migration_round_trip_with_existing_data(
    db_engine: AsyncEngine, session_factory: Factory
) -> None:
    """在含已有事实、草稿、匹配报告、多版本简历的库上验证升级、回退、再升级。"""
    user_id = await add_user(session_factory, "mig@criteria.invalid")
    doc_id = await add_document(session_factory, user_id)
    fact_set_id = await add_fact_set(session_factory, user_id, 1)
    match_run_id = uuid4()
    tailor_run_id = uuid4()
    resume_id = uuid4()
    v1_id, v2_id = uuid4(), uuid4()

    async with session_factory.begin() as db:
        db.add(resume_fact(user_id, fact_set_id, doc_id))
        db.add(
            FactDraft(
                user_id=user_id,
                base_fact_set_id=fact_set_id,
                revision=2,
                items_json=[{"text": "草稿事实"}],
            )
        )
        db.add(
            AiRun(
                run_id=match_run_id,
                user_id=user_id,
                kind="match",
                fact_set_id=fact_set_id,
                job_snapshot_json={"requirements": []},
            )
        )
        db.add(
            AiRun(
                run_id=tailor_run_id,
                user_id=user_id,
                kind="tailor",
                fact_set_id=fact_set_id,
                base_doc_id=doc_id,
            )
        )
        await db.flush()
        db.add(MatchReport(user_id=user_id, run_id=match_run_id, items_json=[]))
        db.add(
            TailoredResume(
                resume_id=resume_id,
                run_id=tailor_run_id,
                user_id=user_id,
                current_version_id=v2_id,
            )
        )
        db.add_all(
            [
                ResumeVersion(
                    resume_version_id=v1_id,
                    resume_id=resume_id,
                    user_id=user_id,
                    version=1,
                    content_json={"header": [], "sections": []},
                ),
                ResumeVersion(
                    resume_version_id=v2_id,
                    resume_id=resume_id,
                    user_id=user_id,
                    version=2,
                    content_json={"header": ["第二版"], "sections": []},
                ),
            ]
        )

    async with db_engine.connect() as connection:
        await connection.run_sync(_verify_migration_round_trip)
        await connection.rollback()


def _verify_migration_round_trip(connection: Connection) -> None:
    connection.begin()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["connection"] = connection
    fact_query = text("SELECT fact_id, text FROM fact")
    before_fact = connection.execute(fact_query).one()

    command.downgrade(config, "20261125_01")
    schema = inspect(connection)
    assert not schema.has_table("fact_set_criterion")
    assert not schema.has_table("fact_set_education")

    command.upgrade(config, "head")
    schema = inspect(connection)
    assert schema.has_table("fact_set_criterion")
    assert schema.has_table("fact_set_education")
    assert connection.execute(fact_query).one() == before_fact
    assert connection.scalar(text("SELECT revision FROM fact_draft")) == 2
    assert connection.scalar(text("SELECT count(*) FROM match_report")) == 1
    assert connection.scalar(text("SELECT count(*) FROM resume_version")) == 2

    with pytest.raises(DBAPIError, match="append-only"), connection.begin_nested():
        connection.execute(text("UPDATE fact SET text = '修改'"))

    command.check(config)
