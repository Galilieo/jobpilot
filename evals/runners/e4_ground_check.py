"""E4 第一步：对评测句子跑依据检查三层，把每层结果写入 evals/results/。

第一、二层是程序，结果固定，每句只算一次；第三层调用真实模型，会产生少量费用，
每句都调（不按 should_run_semantic 跳过），这样三种组合可以分别计分。
在仓库根目录、数据库已启动时运行：

    uv run --project jobpilot python -m evals.runners.e4_ground_check
    uv run --project jobpilot python -m evals.runners.e4_ground_check --test --runs 3

数据格式见 evals/README.md 的「E4 依据检查」。
"""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from evals.runners.e1_extract_facts import eval_user_id, git_state
from jobpilot.config import REPO_ROOT, ModelPurpose, settings
from jobpilot.db.models import (
    AiRun,
    FactSource,
    ModelCall,
    Responsibility,
    RunStatus,
)
from jobpilot.db.runs import set_run_status
from jobpilot.db.session import create_database_engine
from jobpilot.services.grounding import (
    STRUCTURE_KINDS,
    BulletSection,
    GroundingFact,
    check_grounding,
    organizations_of,
)
from jobpilot.services.llm_gateway import ModelBudget
from jobpilot.services.prompts import load_prompt
from jobpilot.services.semantic_check import ground_check

E4_DIR = REPO_ROOT / "evals" / "datasets" / "e4"
ANNOTATION_DIR = REPO_ROOT / "evals" / "datasets" / "annotations"
RESULT_DIR = REPO_ROOT / "evals" / "results"


def fact_id(sample_id: str, index: int) -> UUID:
    """同一简历同一条事实每次得到同一个 ID，结果文件之间可以对照。"""
    return uuid5(NAMESPACE_URL, f"jobpilot-e4:{sample_id}#{index}")


def load_cases(split: str) -> list[dict[str, Any]]:
    path = E4_DIR / f"cases-{split}.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def load_fact_set(sample_id: str) -> dict[UUID, GroundingFact]:
    """把 E1 标注里的事实当作这份简历已发布的事实库。"""
    annotation = json.loads(
        (ANNOTATION_DIR / f"{sample_id}.json").read_text(encoding="utf-8")
    )
    return {
        fact_id(sample_id, index): GroundingFact(
            fact_id=fact_id(sample_id, index),
            experience=fact["experience"],
            responsibility=Responsibility(fact["responsibility"]),
            text=fact["text"],
            source=FactSource.RESUME,
            quote_verified=True,
        )
        for index, fact in enumerate(annotation["facts"])
    }


def program_layers(case: dict[str, Any], facts: dict[UUID, GroundingFact]) -> dict:
    ids = [fact_id(case["sample_id"], index) for index in case["fact_indexes"]]
    result = check_grounding(
        case["sentence"],
        ids,
        facts,
        section=BulletSection(case["section"]),
        experience=case.get("experience"),
        known_organizations=organizations_of({f.experience for f in facts.values()}),
    )
    issues = [issue.model_dump(mode="json") for issue in result.issues]
    return {
        "layer1": [i for i in issues if i["kind"] in STRUCTURE_KINDS],
        "layer2": [i for i in issues if i["kind"] not in STRUCTURE_KINDS],
    }


async def semantic_layer(
    session_factory: async_sessionmaker[AsyncSession],
    client: AsyncOpenAI,
    user_id: Any,
    case: dict[str, Any],
    facts: dict[UUID, GroundingFact],
) -> dict[str, Any]:
    cited = [facts[fact_id(case["sample_id"], i)] for i in case["fact_indexes"]]
    async with session_factory.begin() as session:
        run = AiRun(user_id=user_id, kind="tailor")
        session.add(run)

    record: dict[str, Any] = {"error": None}
    try:
        result = await ground_check(
            session_factory,
            user_id,
            run.run_id,
            client=client,
            budget=ModelBudget(remaining_calls=3, remaining_tokens=20_000),
            sentence=case["sentence"],
            facts=cited,
        )
        record["verdict"] = result.verdict.value if result.verdict else None
        record["issues"] = [issue.model_dump(mode="json") for issue in result.issues]
        status = RunStatus.SUCCEEDED
    except Exception as error:  # 评测要记下任何失败原因，再继续跑下一句
        record["error"] = f"{type(error).__name__}: {error}"
        record["verdict"] = None
        record["issues"] = []
        status = RunStatus.FAILED

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


async def main(split: str, runs: int) -> int:
    api_key = settings.deepseek_api_key.get_secret_value()
    if not api_key:
        print("未配置 OPENAI_API_KEY，请在仓库根目录 .env 中填写后重试。")
        return 1

    cases = load_cases(split)
    fact_sets = {s: load_fact_set(s) for s in {case["sample_id"] for case in cases}}
    output: dict[str, Any] = {
        "eval": "E4",
        "split": split,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "git": git_state(),
        "prompt_version": load_prompt("ground_check").label,
        "model": settings.model_for_purpose(ModelPurpose.GROUND_CHECK).model_dump(),
        "runs": runs,
        "cases": [
            case | program_layers(case, fact_sets[case["sample_id"]]) for case in cases
        ],
        "semantic": [],
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
                for case in cases:
                    record = await semantic_layer(
                        session_factory,
                        client,
                        user_id,
                        case,
                        fact_sets[case["sample_id"]],
                    )
                    record |= {"case_id": case["case_id"], "run_index": run_index}
                    output["semantic"].append(record)
                    outcome = record["error"] or record["verdict"]
                    print(f"第 {run_index + 1} 次 {case['case_id']}：{outcome}")
    finally:
        await engine.dispose()

    RESULT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = RESULT_DIR / f"e4-{split}-{stamp}.json"
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结果已写入 {path.relative_to(REPO_ROOT)}")
    return 1 if any(r["error"] for r in output["semantic"]) else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--test", action="store_true", help="运行测试集")
    parser.add_argument("--runs", type=int, default=3, help="第三层每句运行几次")
    args = parser.parse_args()
    sys.exit(asyncio.run(main("test" if args.test else "dev", args.runs)))
