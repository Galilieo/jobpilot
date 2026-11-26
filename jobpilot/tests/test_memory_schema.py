"""通过 PostgreSQL 验证记忆归属、取值约束和删除账号的行为。"""

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


async def test_memory_checks_list_value_and_cascades_with_account(
    db_session: AsyncSession,
) -> None:
    user_id = uuid4()
    await db_session.execute(
        text("""
            INSERT INTO user_account (user_id, email, hashed_password)
            VALUES (:owner, 'memory-schema@example.invalid', 'unused')
        """),
        {"owner": user_id},
    )
    insert = text("""
        INSERT INTO memory (user_id, key, value_json, revision, source)
        VALUES (:owner, 'cities', CAST(:value AS jsonb), 1, '{"kind":"manual"}')
    """)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(insert, {"owner": user_id, "value": "[42]"})
    await db_session.execute(insert, {"owner": user_id, "value": '["上海"]'})
    await db_session.execute(
        text("DELETE FROM user_account WHERE user_id = :owner"), {"owner": user_id}
    )
    assert (
        await db_session.scalar(
            text("SELECT count(*) FROM memory WHERE user_id = :owner"),
            {"owner": user_id},
        )
        == 0
    )
