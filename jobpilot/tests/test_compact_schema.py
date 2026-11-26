"""PostgreSQL 同时允许一轮对话和一次压缩，拒绝同会话的重复压缩。"""

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


async def test_compact_run_unique_index_is_independent_of_agent(
    db_session: AsyncSession,
) -> None:
    user_id, conversation_id = uuid4(), uuid4()
    await db_session.execute(
        text("""
            INSERT INTO user_account (user_id, email, hashed_password)
            VALUES (:owner, 'compact-schema@example.invalid', 'unused')
        """),
        {"owner": user_id},
    )
    await db_session.execute(
        text("""
            INSERT INTO conversation (conversation_id, user_id, title)
            VALUES (:conversation, :owner, '测试')
        """),
        {"owner": user_id, "conversation": conversation_id},
    )
    insert_run = text("""
        INSERT INTO ai_run (run_id, user_id, conversation_id, kind)
        VALUES (:run, :owner, :conversation, :kind)
    """)
    for kind in ("agent", "compact"):
        await db_session.execute(
            insert_run,
            {
                "run": uuid4(),
                "owner": user_id,
                "conversation": conversation_id,
                "kind": kind,
            },
        )
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                insert_run,
                {
                    "run": uuid4(),
                    "owner": user_id,
                    "conversation": conversation_id,
                    "kind": "compact",
                },
            )
