"""在独立 schema 中连接 LangGraph 检查点，并随业务数据清理线程。"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import AsyncSession

from jobpilot.config import settings

_SCHEMA = "jobpilot_checkpoint"
_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


@asynccontextmanager
async def checkpoint_saver(database_url: URL) -> AsyncIterator[AsyncPostgresSaver]:
    """检查点连接独立于业务事务，连接参数沿用同一个数据库。"""
    connection = make_conninfo(
        **database_url.translate_connect_args(username="user", database="dbname"),
        options=f"-c search_path={_SCHEMA}",
        connect_timeout=settings.database_connect_timeout_seconds,
    )
    async with await AsyncConnection.connect(
        connection, autocommit=True, prepare_threshold=0, row_factory=dict_row
    ) as db:
        saver = AsyncPostgresSaver(db)
        await db.execute(
            "SELECT pg_advisory_lock(hashtext('jobpilot_checkpoint_setup'))"
        )
        try:
            await saver.setup()
        finally:
            await db.execute(
                "SELECT pg_advisory_unlock(hashtext('jobpilot_checkpoint_setup'))"
            )
        yield saver


async def delete_checkpoint_threads(
    session: AsyncSession, thread_ids: Sequence[str]
) -> None:
    """和业务删除一起提交。尚未初始化的 LangGraph 表没有线程数据。"""
    if not thread_ids:
        return
    for table in _TABLES:
        qualified = f"{_SCHEMA}.{table}"
        if await session.scalar(text("SELECT to_regclass(:name)"), {"name": qualified}):
            await session.execute(
                text(f"DELETE FROM {qualified} WHERE thread_id = ANY(:thread_ids)"),
                {"thread_ids": list(thread_ids)},
            )
