"""E1 第一步：用真实模型拆分开发集简历，把完整输出写入 evals/results/。

会产生少量真实费用，只在手动运行时调用，不进入 pytest。
在仓库根目录、数据库已启动时运行：

    uv run --project jobpilot python -m evals.runners.e1_extract_facts
    uv run --project jobpilot python -m evals.runners.e1_extract_facts \
        --runs 1 resume-003
    uv run --project jobpilot python -m evals.runners.e1_extract_facts resume-019
    uv run --project jobpilot python -m evals.runners.e1_extract_facts --test --runs 5

倒数第二条单独运行英文探针，它不在默认的开发集里。最后一条在测试集上报告最终结果，
提示词定版后才运行，测试集的结果不用来修改提示词或规则。

运行和计分分开：输出保存下来以后，改计分规则只需重新计分，不必再调用模型。
调用记录写入开发库，归属一个固定的评测账号。
"""

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from jobpilot.config import REPO_ROOT, ModelPurpose, settings
from jobpilot.db.accounts import find_account_by_email
from jobpilot.db.models import AiRun, ModelCall, RunStatus, User
from jobpilot.db.runs import set_run_status
from jobpilot.db.session import create_database_engine
from jobpilot.services.facts import extract_facts
from jobpilot.services.llm_gateway import ModelBudget
from jobpilot.services.parsing import normalize_text
from jobpilot.services.prompts import load_prompt
from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

EVAL_EMAIL = "e1-eval@example.invalid"
RESUME_DIR = REPO_ROOT / "evals" / "datasets" / "resumes"
RESULT_DIR = REPO_ROOT / "evals" / "results"
# 开发集：5 份短样本和 5 份完整简历。英文探针 resume-019 需要单独指定。
DEV_SAMPLES = [
    *(f"resume-00{n}" for n in range(1, 6)),
    *(f"resume-0{n}" for n in (11, 15, 21, 22, 23)),
]
# 测试集：5 份短样本和 7 份完整简历，分组见 evals/README.md。
TEST_SAMPLES = [
    *(f"resume-0{n:02d}" for n in range(6, 11)),
    *(f"resume-0{n}" for n in (12, 13, 14, 16, 17, 18, 20)),
]

# 一次运行的额度：允许一次 JSON 修复重发。
# token 按网关的估算方式（输入字节数 + max_tokens）留足。
CALLS_PER_SAMPLE = 3
TOKENS_PER_SAMPLE = 60_000


def git_state() -> dict[str, Any]:
    """记录代码版本：有未提交的改动时，提交号不足以复现这次运行。"""

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()

    return {
        "commit": git("rev-parse", "HEAD"),
        "dirty": bool(git("status", "--porcelain", "--", "jobpilot")),
    }


async def eval_user_id(session_factory: async_sessionmaker[AsyncSession]) -> Any:
    """找到或创建评测账号。占位值不是合法哈希，这个账号无法登录。"""
    async with session_factory.begin() as session:
        user = await find_account_by_email(session, EVAL_EMAIL)
        if user is None:
            user = User(email=EVAL_EMAIL, hashed_password="e1-not-a-login")
            session.add(user)
            await session.flush()
        return user.user_id


async def run_sample(
    session_factory: async_sessionmaker[AsyncSession],
    client: AsyncOpenAI,
    user_id: Any,
    sample_id: str,
    raw_text: str,
) -> dict[str, Any]:
    """拆分一份简历。失败也返回记录，失败本身是评测结果的一部分。"""
    async with session_factory.begin() as session:
        run = AiRun(user_id=user_id, kind="parse_resume")
        session.add(run)

    record: dict[str, Any] = {"sample_id": sample_id, "run_id": str(run.run_id)}
    status = RunStatus.FAILED
    try:
        result = await extract_facts(
            session_factory,
            user_id,
            run.run_id,
            client=client,
            budget=ModelBudget(
                remaining_calls=CALLS_PER_SAMPLE, remaining_tokens=TOKENS_PER_SAMPLE
            ),
            doc_id=uuid4(),
            raw_text=raw_text,
        )
        record["error"] = None
        record["sections"] = [s.model_dump(mode="json") for s in result.sections]
        record["facts"] = [f.model_dump(mode="json") for f in result.facts]
        record["criteria"] = [c.model_dump(mode="json") for c in result.criteria]
        record["education"] = [e.model_dump(mode="json") for e in result.education]
        status = RunStatus.SUCCEEDED
    except Exception as error:  # 评测要记下任何失败原因，再继续跑下一份
        record["error"] = f"{type(error).__name__}: {error}"
        record["sections"] = []
        record["facts"] = []
        record["criteria"] = []
        record["education"] = []

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

    # 与产品一致：模型看到的是规范化后的文本，位置也按它计算。
    texts = {
        sample_id: normalize_text(
            (RESUME_DIR / f"{sample_id}.txt").read_text(encoding="utf-8")
        )
        for sample_id in sample_ids
    }
    output: dict[str, Any] = {
        "eval": "E1",
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "git": git_state(),
        "prompt_version": load_prompt("extract_facts").label,
        "model": settings.model_for_purpose(ModelPurpose.EXTRACT_FACTS).model_dump(),
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
                    outcome = record["error"] or f"{len(record['facts'])} 条事实"
                    print(f"第 {run_index + 1} 次 {sample_id}：{outcome}")
    finally:
        await engine.dispose()

    RESULT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = RESULT_DIR / f"e1-{stamp}.json"
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结果已写入 {path.relative_to(REPO_ROOT)}")
    failed = sum(1 for record in output["results"] if record["error"])
    return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("samples", nargs="*", help="简历编号，默认开发集")
    parser.add_argument("--test", action="store_true", help="运行测试集")
    parser.add_argument("--runs", type=int, default=3, help="每份简历运行几次")
    args = parser.parse_args()
    if args.test and args.samples:
        parser.error("--test 与指定简历编号不能同时使用")
    samples = args.samples or (TEST_SAMPLES if args.test else DEV_SAMPLES)
    sys.exit(asyncio.run(main(samples, args.runs)))
