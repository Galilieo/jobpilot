"""验证 ai_run.ck_ai_run_stop_reason 收紧迁移：发现违规行时报错且不改数据。"""

from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from alembic import command
from jobpilot.tests.test_fact_schema import Factory, add_user


async def test_stop_reason_migration_rejects_contradictory_rows_and_round_trips(
    db_engine: AsyncEngine, session_factory: Factory
) -> None:
    user_id = await add_user(session_factory, "stop-migration@example.invalid")
    async with db_engine.connect() as connection:
        await connection.run_sync(_check_migration, user_id)
        await connection.rollback()


def _check_migration(connection: Connection, user_id) -> None:
    connection.begin()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["connection"] = connection

    command.downgrade(config, "20261125_02")
    bad_run_id = uuid4()
    connection.execute(
        text(
            "INSERT INTO ai_run "
            "(run_id, user_id, kind, status, stop_reason, finished_at) "
            "VALUES (:run_id, :user_id, 'tailor', 'succeeded', "
            "'rewrite_failed', now())"
        ),
        {"run_id": bad_run_id, "user_id": user_id},
    )

    with pytest.raises(RuntimeError, match="1"):
        command.upgrade(config, "head")

    # 迁移失败后数据不能被悄悄改掉
    row = connection.execute(
        text("SELECT status, stop_reason FROM ai_run WHERE run_id = :run_id"),
        {"run_id": bad_run_id},
    ).one()
    assert tuple(row) == ("succeeded", "rewrite_failed")

    connection.execute(
        text("DELETE FROM ai_run WHERE run_id = :run_id"),
        {"run_id": bad_run_id},
    )
    connection.commit()
    connection.begin()
    command.upgrade(config, "head")
    command.downgrade(config, "20261125_02")
    command.upgrade(config, "head")
    command.check(config)
