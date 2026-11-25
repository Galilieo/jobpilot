"""用真实 PostgreSQL 验证匹配报告的组合外键、唯一约束与级联删除。"""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import delete, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alembic import command
from jobpilot.db.models import (
    AiRun,
    Fact,
    FactSet,
    JobPosting,
    MatchReport,
    User,
    UserJob,
)
from jobpilot.db.reports import add_match_report
from jobpilot.tests.test_fact_schema import (
    add_document,
    add_fact_set,
    add_user,
    resume_fact,
    violated_constraint,
)


async def test_match_report_constraints_and_snapshot_persistence(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """跨用户引用 run、同一 run 建两份报告均被拒绝；同用户可写入并持久化快照。"""
    user_a = await add_user(session_factory, "a@match.invalid")
    user_b = await add_user(session_factory, "b@match.invalid")
    set_a = await add_fact_set(session_factory, user_a, 1)
    snapshot = {
        "basic_fields": {"title": "后端开发实习生"},
        "requirements": [
            {
                "requirement_id": str(uuid4()),
                "type": "hard",
                "text": "熟悉 Python",
            }
        ],
        "duties": [],
        "keywords": ["Python"],
    }
    async with session_factory.begin() as session:
        posting = JobPosting(
            owner_id=user_a,
            raw_text="岗位原文",
            content_hash="a" * 64,
            parsed_json=snapshot,
        )
        session.add(posting)
        await session.flush()
        user_job = UserJob(user_id=user_a, posting_id=posting.posting_id)
        session.add(user_job)
        await session.flush()
        run_a = AiRun(
            user_id=user_a,
            kind="match",
            user_job_id=user_job.user_job_id,
            fact_set_id=set_a,
            job_snapshot_json=snapshot,
        )
        session.add(run_a)
        await session.flush()
        run_id = run_a.run_id

    # 跨用户引用 A 的运行必须被组合外键拒绝
    assert (
        await violated_constraint(
            session_factory,
            MatchReport(user_id=user_b, run_id=run_id, items_json=[], main_gaps=[]),
        )
        == "fk_match_report_run_id_ai_run"
    )

    # 同用户正常写入且快照可完整读回
    async with session_factory.begin() as session:
        report = MatchReport(
            user_id=user_a,
            run_id=run_id,
            items_json=[{"requirement_id": str(uuid4()), "verdict": "missing"}],
            main_gaps=["缺少相关项目"],
        )
        session.add(report)
        await session.flush()
        report_id = report.report_id

    async with session_factory() as reader:
        saved_run = await reader.get(AiRun, run_id)
        saved_report = await reader.get(MatchReport, report_id)
        assert saved_run is not None
        assert saved_run.job_snapshot_json == snapshot
        assert saved_report is not None
        assert saved_report.main_gaps == ["缺少相关项目"]

    # 同一 run_id 不能生成第二份报告
    assert (
        await violated_constraint(
            session_factory,
            MatchReport(user_id=user_a, run_id=run_id, items_json=[], main_gaps=[]),
        )
        == "uq_match_report_run_id"
    )


async def test_deleting_user_cascades_across_match_reports(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """删除账号时，在同一事务内级联删除匹配报告、运行、岗位和事实库。"""
    user = await add_user(session_factory, "cascade@match.invalid")
    doc = await add_document(session_factory, user)
    fact_set = await add_fact_set(session_factory, user, 1)
    async with session_factory.begin() as session:
        session.add(resume_fact(user, fact_set, doc))
        posting = JobPosting(
            owner_id=user,
            raw_text="岗位原文",
            content_hash="b" * 64,
            parsed_json={
                "basic_fields": {},
                "requirements": [],
                "duties": [],
                "keywords": [],
            },
        )
        session.add(posting)
        await session.flush()
        user_job = UserJob(user_id=user, posting_id=posting.posting_id)
        session.add(user_job)
        await session.flush()
        run = AiRun(
            user_id=user,
            kind="match",
            user_job_id=user_job.user_job_id,
            fact_set_id=fact_set,
            job_snapshot_json={"requirements": []},
        )
        session.add(run)
        await session.flush()
        session.add(
            MatchReport(user_id=user, run_id=run.run_id, items_json=[], main_gaps=[])
        )

    async with session_factory.begin() as session:
        await session.execute(delete(User).where(User.user_id == user))

    async with session_factory() as reader:
        for table in (MatchReport, AiRun, UserJob, JobPosting, Fact, FactSet):
            assert (await reader.scalars(select(table))).all() == []


@pytest.mark.parametrize("missing", ["kind", "fact_set", "snapshot"])
async def test_report_requires_complete_match_run(
    session_factory: async_sessionmaker[AsyncSession], missing: str
) -> None:
    owner = await add_user(session_factory, "owner@report.invalid")
    version = await add_fact_set(session_factory, owner, 1)
    async with session_factory.begin() as db:
        run = AiRun(
            user_id=owner,
            kind="parse_resume" if missing == "kind" else "match",
            fact_set_id=None if missing == "fact_set" else version,
            job_snapshot_json=None if missing == "snapshot" else {"requirements": []},
        )
        db.add(run)
        await db.flush()
        with pytest.raises(ValueError, match="匹配运行"):
            add_match_report(db, run, items=[], main_gaps=[])
        assert await db.scalar(select(MatchReport)) is None


async def test_match_report_seq_migration_backfills_and_round_trips(
    db_engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """降级插入两条历史报告，升级后按时间与 ID 回填 seq，新插入继续递增。"""
    owner = await add_user(session_factory, "seq-migration@example.invalid")
    version = await add_fact_set(session_factory, owner, 1)
    async with db_engine.connect() as connection:
        await connection.run_sync(_check_seq_migration, owner, version)
        await connection.rollback()


def _check_seq_migration(connection: Connection, owner, version) -> None:
    connection.begin()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["connection"] = connection

    command.downgrade(config, "20261125_03")
    t1 = datetime(2026, 11, 25, 10, 0, 0, tzinfo=UTC)
    t2 = datetime(2026, 11, 25, 10, 1, 0, tzinfo=UTC)
    r1, r2, r3 = uuid4(), uuid4(), uuid4()
    rep1, rep2, rep3 = uuid4(), uuid4(), uuid4()
    for rid in (r1, r2, r3):
        connection.execute(
            text(
                "INSERT INTO ai_run "
                "(run_id, user_id, kind, fact_set_id, job_snapshot_json) "
                "VALUES (:run_id, :user_id, 'match', :fact_set_id, '{}'::jsonb)"
            ),
            {"run_id": rid, "user_id": owner, "fact_set_id": version},
        )
    # 故意先插入较晚时间、后插入较早时间，验证按 created_at ASC, report_id ASC 回填
    connection.execute(
        text(
            "INSERT INTO match_report (report_id, user_id, run_id, created_at) "
            "VALUES (:rep2, :user_id, :r2, :t2), (:rep1, :user_id, :r1, :t1)"
        ),
        {
            "rep1": rep1,
            "rep2": rep2,
            "user_id": owner,
            "r1": r1,
            "r2": r2,
            "t1": t1,
            "t2": t2,
        },
    )

    command.upgrade(config, "head")
    rows = connection.execute(
        text("SELECT report_id, seq FROM match_report ORDER BY seq ASC")
    ).all()
    assert [(row[0], row[1]) for row in rows] == [(rep1, 1), (rep2, 2)]

    # 升级后再插入新报告，自增序号应紧接着从 3 开始
    connection.execute(
        text(
            "INSERT INTO match_report (report_id, user_id, run_id) "
            "VALUES (:rep3, :user_id, :r3)"
        ),
        {"rep3": rep3, "user_id": owner, "r3": r3},
    )
    seq3 = connection.execute(
        text("SELECT seq FROM match_report WHERE report_id = :rep3"),
        {"rep3": rep3},
    ).scalar_one()
    assert seq3 == 3

    command.downgrade(config, "20261125_03")
    command.upgrade(config, "head")
    command.check(config)
