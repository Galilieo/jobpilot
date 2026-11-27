"""验证幂等键迁移与已有事实、草稿数据的兼容性。"""

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from alembic import command
from jobpilot.db.models import FactDraft
from jobpilot.tests.test_fact_schema import (
    Factory,
    add_document,
    add_fact_set,
    add_user,
    resume_fact,
)


async def test_idempotency_revision_round_trip(
    db_engine: AsyncEngine, session_factory: Factory
) -> None:
    user_id = await add_user(session_factory, "idempotency-migration@example.invalid")
    doc_id = await add_document(session_factory, user_id)
    fact_set_id = await add_fact_set(session_factory, user_id, 1)
    async with session_factory.begin() as db:
        db.add(resume_fact(user_id, fact_set_id, doc_id))
        db.add(FactDraft(user_id=user_id, base_fact_set_id=fact_set_id, revision=3))

    async with db_engine.connect() as connection:
        await connection.run_sync(_check_revision)
        await connection.rollback()


async def test_retired_idempotency_key_migration_round_trip(
    db_engine: AsyncEngine, session_factory: Factory
) -> None:
    user_id = await add_user(session_factory, "retired-key@example.invalid")
    async with db_engine.connect() as connection:
        await connection.run_sync(_check_retired_key, user_id)
        await connection.rollback()


def _check_retired_key(connection: Connection, user_id: UUID) -> None:
    connection.begin()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["connection"] = connection
    insert_key = text(
        "INSERT INTO idempotency_key "
        "(user_id, operation, key, request_hash, attempt_id, status) "
        "VALUES (:user, 'parse_jd', :key, :hash, :attempt, 'retired')"
    )
    values = {
        "user": user_id,
        "key": "deleted-job",
        "hash": "a" * 64,
        "attempt": uuid4(),
    }
    connection.execute(insert_key, values)
    with (
        pytest.raises(DBAPIError, match="ck_idempotency_key_response"),
        connection.begin_nested(),
    ):
        connection.execute(text("UPDATE idempotency_key SET response_body = '{}'"))
    command.downgrade(config, "20261127_01")
    assert connection.scalar(text("SELECT count(*) FROM idempotency_key")) == 0
    command.upgrade(config, "head")
    connection.execute(insert_key, values)
    command.check(config)


def _check_revision(connection: Connection) -> None:
    connection.begin()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["connection"] = connection
    fact_query = text("SELECT fact_id, text FROM fact")
    before = connection.execute(fact_query).one()

    command.downgrade(config, "20261124_03")
    assert not inspect(connection).has_table("idempotency_key")
    command.upgrade(config, "head")

    pk = inspect(connection).get_pk_constraint("idempotency_key")
    assert pk["constrained_columns"] == ["user_id", "operation", "key"]
    assert connection.execute(fact_query).one() == before
    assert connection.scalar(text("SELECT revision FROM fact_draft")) == 3
    with pytest.raises(DBAPIError, match="append-only"), connection.begin_nested():
        connection.execute(text("UPDATE fact SET text = '新的文字'"))
    command.check(config)
