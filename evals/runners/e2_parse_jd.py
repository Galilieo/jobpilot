"""E2 第一步：用真实模型解析开发集或测试集的 JD，把完整输出写入 evals/results/。

会产生少量真实费用，只在手动运行时调用，不进入 pytest。
在仓库根目录、数据库已启动时运行：

    uv run --project jobpilot python -m evals.runners.e2_parse_jd
    uv run --project jobpilot python -m evals.runners.e2_parse_jd --test --runs 5

分组见 evals/datasets/e2-split.json。结果文件含 JD 原文，evals/results/ 已被 Git 忽略。
运行和计分分开：改计分规则只需重新运行 e2_score，不必再调用模型。
"""

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import UTC, datetime
from typing import Any

from evals.runners.e1_extract_facts import eval_user_id, git_state
from jobpilot.config import REPO_ROOT, ModelPurpose, settings
from jobpilot.db.models import AiRun, ModelCall, RunStatus
from jobpilot.db.runs import set_run_status
from jobpilot.db.session import create_database_engine
from jobpilot.services.jobs import parse_job
from jobpilot.services.llm_gateway import ModelBudget
from jobpilot.services.parsing import normalize_text
from jobpilot.services.prompts import load_prompt
from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

JD_DIR = REPO_ROOT / "evals" / "private" / "jds"
RESULT_DIR = REPO_ROOT / "evals" / "results"
SPLIT = REPO_ROOT / "evals" / "datasets" / "e2-split.json"


async def run_sample(
    session_factory: async_sessionmaker[AsyncSession],
    client: AsyncOpenAI,
    user_id: Any,
    sample_id: str,
    raw_text: str,
) -> dict[str, Any]:
    """解析一份 JD。失败也返回记录，失败本身是评测结果的一部分。"""
    async with session_factory.begin() as session:
        run = AiRun(user_id=user_id, kind="parse_jd")
        session.add(run)

    record: dict[str, Any] = {"sample_id": sample_id, "run_id": str(run.run_id)}
    status = RunStatus.FAILED
    try:
        parsed = await parse_job(
            session_factory,
            user_id,
            run.run_id,
            client=client,
            budget=ModelBudget(remaining_calls=3, remaining_tokens=40_000),
            raw_text=raw_text,
        )
        record["error"] = None
        record["parsed"] = parsed.model_dump(mode="json")
        status = RunStatus.SUCCEEDED
    except Exception as error:  # 评测要记下任何失败原因，再继续跑下一份
        record["error"] = f"{type(error).__name__}: {error}"
        record["parsed"] = None

    async with session_factory.begin() as session:
        await set_run_status(session, user_id, run.run_id, status)
        calls = (
            await session.scalars(
                select(ModelCall).where(ModelCall.run_id == run.run_id)
            )
        ).all()
    record["calls"] = [
        {
            "outcome": str(call.outcome),
            "latency_ms": call.latency_ms,
            "prompt_tokens": call.prompt_tokens,
            "completion_tokens": call.completion_tokens,
        }
        for call in calls
    ]
    return record


async def main(sample_ids: list[str], runs: int) -> int:
    api_key = settings.deepseek_api_key.get_secret_value()
    if not api_key:
        print("未配置 OPENAI_API_KEY，请在仓库根目录 .env 中填写后重试。")
        return 1

    texts = {
        sample_id: normalize_text(
            (JD_DIR / f"{sample_id}.txt").read_text(encoding="utf-8")
        )
        for sample_id in sample_ids
    }
    output: dict[str, Any] = {
        "eval": "E2",
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "git": git_state(),
        "prompt_version": load_prompt("parse_jd").label,
        "model": settings.model_for_purpose(ModelPurpose.PARSE_JD).model_dump(),
        "runs": runs,
        "inputs": {
            sample_id: {
                "raw_text": text,
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
            for sample_id, text in texts.items()
        },
        "results": [],
    }

    engine = create_database_engine(
        settings.database_url,
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        user_id = await eval_user_id(session_factory)
        async with AsyncOpenAI(
            api_key=api_key, base_url=settings.deepseek_base_url
        ) as client:
            for run_index in range(runs):
                for sample_id in sample_ids:
                    record = await run_sample(
                        session_factory, client, user_id, sample_id, texts[sample_id]
                    )
                    record["run_index"] = run_index
                    output["results"].append(record)
                    parsed = record["parsed"]
                    outcome = record["error"] or f"{len(parsed['requirements'])} 条要求"
                    print(f"第 {run_index + 1} 次 {sample_id}：{outcome}")
    finally:
        await engine.dispose()

    RESULT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = RESULT_DIR / f"e2-{stamp}.json"
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结果已写入 {path.relative_to(REPO_ROOT)}")
    return 1 if any(record["error"] for record in output["results"]) else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("samples", nargs="*", help="JD 编号，默认开发集")
    parser.add_argument("--test", action="store_true", help="运行测试集")
    parser.add_argument("--runs", type=int, default=3, help="每份 JD 运行几次")
    args = parser.parse_args()
    if args.test and args.samples:
        parser.error("--test 与指定 JD 编号不能同时使用")
    split = json.loads(SPLIT.read_text(encoding="utf-8"))
    samples = args.samples or split["test" if args.test else "dev"]
    sys.exit(asyncio.run(main(samples, args.runs)))
