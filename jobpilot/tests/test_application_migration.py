"""检查投递迁移在升级和降级时生成对应的 PostgreSQL DDL。"""

from io import StringIO
from pathlib import Path
from types import ModuleType

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncEngine

from alembic import command


def test_application_migration_emits_reversible_ddl() -> None:
    output = StringIO()
    config = Config(
        str(Path(__file__).resolve().parents[1] / "alembic.ini"), output_buffer=output
    )
    command.upgrade(config, "20261126_06:20261127_01", sql=True)
    upgrade = output.getvalue()
    assert "CREATE TABLE application (" in upgrade
    assert "CREATE TABLE application_stage_event (" in upgrade
    assert "ON DELETE SET NULL (user_job_id)" in upgrade
    assert "ON DELETE SET NULL (resume_version_id)" in upgrade
    assert "ALTER TABLE user_job DROP COLUMN stage" in upgrade

    output.seek(0)
    output.truncate()
    command.downgrade(config, "20261127_01:20261126_06", sql=True)
    downgrade = output.getvalue()
    assert "ALTER TABLE user_job ADD COLUMN stage" in downgrade
    assert "DROP TABLE application_stage_event" in downgrade
    assert "DROP TABLE application" in downgrade


async def test_legacy_job_stages_survive_migration_round_trip(
    db_engine: AsyncEngine,
) -> None:
    """在测试库事务内验证带旧数据的升级、降级和再升级，最后回滚。"""
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    migration = ScriptDirectory.from_config(config).get_revision("20261127_01").module
    async with db_engine.connect() as connection:
        await connection.run_sync(_round_trip, migration)
        await connection.rollback()


def _round_trip(connection: Connection, migration: ModuleType) -> None:
    with Operations.context(MigrationContext.configure(connection)):
        migration.downgrade()
        _seed_legacy_jobs(connection)
        migration.upgrade()
        _assert_backfill(connection)
        connection.execute(
            text(
                "UPDATE application SET stage = 'written_test' WHERE stage = 'applied'"
            )
        )
        connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        migration.downgrade()
        assert set(connection.scalars(text("SELECT stage FROM user_job"))) == {
            "saved",
            "applied",
            "interviewing",
            "offer",
            "rejected",
            "archived",
        }
        migration.upgrade()
        _assert_backfill(connection)


def _seed_legacy_jobs(connection: Connection) -> None:
    connection.execute(
        text("""
        WITH owner AS (
            INSERT INTO user_account (user_id, email, hashed_password)
            VALUES (gen_random_uuid(), 'migration@example.invalid', 'fixture')
            RETURNING user_id
        ), postings AS (
            INSERT INTO job_posting
                (posting_id, owner_id, raw_text, content_hash, parsed_json)
            SELECT gen_random_uuid(), user_id, stage, stage,
                   jsonb_build_object('basic_fields',
                       jsonb_build_object('title', stage))
            FROM owner CROSS JOIN
                unnest(ARRAY['saved', 'applied', 'interviewing',
                             'offer', 'rejected', 'archived']) stage
            RETURNING posting_id, owner_id, raw_text
        )
        INSERT INTO user_job (user_job_id, user_id, posting_id, stage,
                              updated_at, field_overrides_json)
        SELECT gen_random_uuid(), owner_id, posting_id, raw_text,
               '2026-11-26 12:00:00+00',
               '{"basic_fields":{"company":"个人修改"}}'::jsonb
        FROM postings
    """)
    )


def _assert_backfill(connection: Connection) -> None:
    applications = connection.execute(
        text("""
        SELECT a.stage, a.backfilled, a.applied_at::text, a.snapshot_json,
               a.resume_version_id, a.job_snapshot_json->>'company',
               e.from_stage, e.to_stage, e.occurred_at::text
        FROM application a
        JOIN application_stage_event e USING (application_id, user_id)
    """)
    ).all()
    assert set(applications) == {
        (stage, True, "2026-11-26", None, None, "个人修改", None, stage, "2026-11-26")
        for stage in ("applied", "interviewing", "offer", "rejected")
    }
    assert connection.scalars(
        text("""
        SELECT p.raw_text FROM user_job j JOIN job_posting p USING (posting_id)
        WHERE j.archived_at IS NOT NULL
    """)
    ).all() == ["archived"]
