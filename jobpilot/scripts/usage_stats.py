"""只读统计指定账号的使用情况，输出纯数字摘要以核对手填使用记录。"""

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from jobpilot.config import settings
from jobpilot.db.session import create_database_engine
from jobpilot.services.usage_stats import (
    collect_usage_stats,
    format_usage_stats,
    stats_to_dict,
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="只读汇总指定账号的对话、改写、投递与模型用量数字。"
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--email", help="目标账号登录邮箱")
    target.add_argument("--user-id", type=UUID, help="目标账号 UUID")
    parser.add_argument(
        "--json", action="store_true", help="以 JSON 格式输出纯数字结果"
    )
    return parser.parse_args(argv)


async def run_stats(args: argparse.Namespace) -> int:
    engine = create_database_engine(
        settings.database_url,
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
    try:
        stats = await collect_usage_stats(
            async_sessionmaker(engine, expire_on_commit=False),
            email=args.email,
            user_id=args.user_id,
        )
    except SQLAlchemyError as error:
        print(f"无法查询使用统计（{type(error).__name__}）", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()

    if stats is None:
        print("未找到指定账号", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(stats_to_dict(stats), ensure_ascii=False, indent=2))
    else:
        print(format_usage_stats(stats))
    return 0


def main() -> int:
    return asyncio.run(run_stats(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
