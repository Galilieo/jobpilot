"""通过 PostgreSQL 验证记忆约束、历史迁移与账号级联删除。"""

from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from alembic import command


async def test_memory_constraints_and_cascades_with_account(db_session: AsyncSession):
    owner = uuid4()
    await db_session.execute(
        text(
            "INSERT INTO user_account (user_id, email, hashed_password) "
            "VALUES (:owner, 'memory-schema@example.invalid', 'unused')"
        ),
        {"owner": owner},
    )
    stmt = text("""
        INSERT INTO memory (memory_id, user_id, category, content, source)
        VALUES (:id, :owner, :category, :content, '{"kind":"manual"}')
    """)
    for category, content in (
        ("invalid", "合法"),
        ("job_pref", ""),
        ("job_pref", "甲" * 201),
    ):
        with pytest.raises(IntegrityError):
            async with db_session.begin_nested():
                await db_session.execute(
                    stmt,
                    {
                        "id": uuid4(),
                        "owner": owner,
                        "category": category,
                        "content": content,
                    },
                )
    memory = {"id": uuid4(), "owner": owner, "category": "job_pref", "content": "偏好"}
    await db_session.execute(stmt, memory)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                stmt, {**memory, "id": uuid4(), "content": "\t 偏好 \n"}
            )
    await db_session.execute(
        text("UPDATE memory SET deleted_at = now() WHERE memory_id = :id"), memory
    )
    await db_session.execute(stmt, {**memory, "id": uuid4()})
    await db_session.execute(
        text("""
        INSERT INTO writing_preference (user_id, tone, source)
            VALUES (:owner, '平实', '{"kind":"manual"}')
    """),
        memory,
    )
    await db_session.execute(
        text("DELETE FROM user_account WHERE user_id = :owner"), memory
    )
    for table in ("memory", "writing_preference"):
        assert await db_session.scalar(text(f"SELECT count(*) FROM {table}")) == 0


async def test_legacy_memory_round_trip(db_engine: AsyncEngine):
    async with db_engine.connect() as conn:
        await conn.run_sync(_round_trip)
        await conn.rollback()


def _round_trip(conn: Connection):
    conn.begin()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["connection"] = conn
    command.downgrade(config, "20261127_03")
    owner, conversation_id, message_id = uuid4(), uuid4(), uuid4()
    conn.execute(
        text(
            "INSERT INTO user_account (user_id, email, hashed_password) "
            "VALUES (:id, 'legacy@example.invalid', 'unused')"
        ),
        {"id": owner},
    )
    conn.execute(
        text("""
        INSERT INTO memory
            (user_id, key, value_json, revision, source, updated_at, deleted_at)
        SELECT :id, key, value, 5,
            CASE WHEN key = 'tech_terms'
                THEN jsonb_build_object('kind', 'conversation',
                    'conversation_id', CAST(:conv AS uuid),
                    'message_id', CAST(:msg AS uuid))
                ELSE '{"kind":"manual"}'::jsonb END,
            now() + make_interval(secs => elapsed),
            CASE WHEN value IS NULL THEN now() ELSE NULL END
        FROM (VALUES
            ('tone', '"平实"'::jsonb, 1),
            ('bullet_length', NULL, 5),
            ('tech_terms', '"英文原名"'::jsonb, 2),
            ('target_direction', '"Agent 开发"'::jsonb, 1),
            ('cities', '["上海", "杭州"]'::jsonb, 1),
            ('excluded_types', '["外包", "外包", "劳务派遣"]'::jsonb, 1),
            ('job_notes',
                to_jsonb(repeat('甲', 120) || '。' || repeat('乙', 90) || '。'), 1)
        ) AS legacy(key, value, elapsed)
    """),
        {"id": owner, "conv": conversation_id, "msg": message_id},
    )
    expected = {
        "目标方向：Agent 开发",
        "意向城市：上海、杭州",
        "不考虑：外包",
        "不考虑：劳务派遣",
    }
    command.upgrade(config, "head")
    memories = set(conn.scalars(text("SELECT content FROM memory")))
    assert memories == {*expected, "甲" * 120 + "。", "乙" * 90 + "。"}
    writing = conn.execute(
        text(
            "SELECT tone, bullet_length, tech_terms, revision, source "
            "FROM writing_preference"
        )
    ).one()
    assert writing[:4] == ("平实", None, "英文原名", 1)
    assert writing.source["conversation_id"] == str(conversation_id)

    command.downgrade(config, "20261127_03")
    recovered = dict(conn.execute(text("SELECT key, value_json FROM memory")).all())
    assert "bullet_length" not in recovered
    assert recovered["target_direction"] == "Agent 开发"
    assert recovered["cities"] == ["上海", "杭州"]
    assert set(recovered["excluded_types"]) == {"外包", "劳务派遣"}
    assert recovered["tone"] == "平实"
    command.upgrade(config, "head")
    assert expected <= set(conn.scalars(text("SELECT content FROM memory")))
    command.check(config)
