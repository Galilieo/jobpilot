"""手动清理当前配置数据库里到期的账号。"""

import asyncio

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from jobpilot.config import settings
from jobpilot.db.session import create_database_engine
from jobpilot.services.accounts import purge_accounts


async def main() -> int:
    engine = create_database_engine(
        settings.database_url,
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
    try:
        result = await purge_accounts(async_sessionmaker(engine))
    except SQLAlchemyError as error:
        print(f"无法查询到期账号（{type(error).__name__}）")
        return 1
    finally:
        await engine.dispose()
    print(f"已删除 {result.deleted} 个账号，清理失败 {result.failed} 个")
    return int(result.failed > 0)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
