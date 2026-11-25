"""验证用户在测试库中提交后可由新 session 读出，保护逐测试的数据清理，
以及引擎创建的异常不回显语句参数。"""

from uuid import uuid4

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from jobpilot.db.models import ResumeDocument, User


@pytest.mark.parametrize(
    "email",
    ["fixture-check@example.invalid", "fixture-check@example.invalid"],
    ids=["first-test", "next-test"],
)
async def test_committed_user_is_readable_in_new_session(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    email: str,
) -> None:
    """两个测试实例复用相同邮箱，均应从空库开始并能独立提交。"""
    assert await db_session.scalar(select(func.current_database())) == "jobpilot_test"
    assert await db_session.scalar(select(func.count()).select_from(User)) == 0
    user = User(email=email, hashed_password="synthetic-hash-for-persistence-only")
    db_session.add(user)

    await db_session.commit()

    async with session_factory() as reading_session:
        saved_user = await reading_session.get(User, user.user_id)
        assert saved_user is not None
        assert saved_user.email == email


async def test_database_errors_do_not_echo_statement_parameters(
    db_engine: AsyncEngine,
) -> None:
    """写入出错时，异常的说明里不回显语句参数。

    参数里可能是简历正文，它会跟着异常进日志。表名、约束名这类定位信息照常保留。
    """
    private = "ZZ_STATEMENT_PARAMETER_MARK"

    async with db_engine.connect() as connection:
        with pytest.raises(IntegrityError) as failure:
            await connection.execute(
                insert(ResumeDocument).values(user_id=uuid4(), raw_text=private)
            )

    assert private not in str(failure.value)
    assert "resume_document" in str(failure.value)
