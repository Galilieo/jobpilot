"""补齐现有消息的分块与向量，也用于更换 Embedding 模型后的重算。"""

import asyncio
import logging
from dataclasses import asdict
from json import dumps

from sqlalchemy.ext.asyncio import async_sessionmaker

from jobpilot.config import settings
from jobpilot.db.session import create_database_engine
from jobpilot.services.message_index import backfill_message_index


async def main() -> int:
    engine = create_database_engine(
        settings.database_url,
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
    try:
        result = await backfill_message_index(
            async_sessionmaker(engine, expire_on_commit=False)
        )
    finally:
        await engine.dispose()
    print(dumps(asdict(result), ensure_ascii=False))
    return int(result.embedding_unavailable)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(asyncio.run(main()))
