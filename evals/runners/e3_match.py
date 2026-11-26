"""用合成标注直接运行 JobPilot 的匹配服务，将每次尝试写入本地结果。"""

import argparse
import asyncio
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from openai import APIError, AsyncOpenAI
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from evals.runners.e1_extract_facts import eval_user_id, git_state
from evals.runners.e3_dataset import (
    JobSample,
    ResumeSample,
    Split,
    dataset_version,
    labels_path,
    load_job,
    load_labels,
    load_resume,
    load_split,
    pair_id,
)
from jobpilot.config import REPO_ROOT, ModelPurpose, settings
from jobpilot.db.models import AiRun, ModelCall, RunStatus
from jobpilot.db.runs import set_run_status
from jobpilot.db.session import create_database_engine
from jobpilot.services.facts import TruncatedOutputError
from jobpilot.services.llm_gateway import (
    BudgetExhaustedError,
    EmptyModelResponseError,
    ModelBudget,
)
from jobpilot.services.matching import BuiltMatchReport, MatchValidationError, match_job
from jobpilot.services.prompts import load_prompt

RESULT_DIR = REPO_ROOT / "evals" / "results"
OFFICIAL_TEST_RUNS = 3
TOKEN_BUDGET_PER_PAIR = 80_000


def _serialize_items(
    report: BuiltMatchReport, job: JobSample, resume: ResumeSample
) -> list[dict[str, Any]]:
    items = []
    for item in report.items:
        record = item.model_dump(mode="json", exclude={"fact_ids", "criteria_refs"})
        record["requirement_id"] = job.requirement_ids[item.requirement_id]
        refs = [*(str(fact_id) for fact_id in item.fact_ids), *item.criteria_refs]
        record["evidence"] = [resume.evidence_ids[ref] for ref in refs]
        items.append(record)
    return items


async def run_pair(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: UUID,
    client: AsyncOpenAI,
    resume: ResumeSample,
    job: JobSample,
) -> dict[str, Any]:
    """登记 ai_run 让真实网关记账，事实库和岗位只在内存组装。"""
    async with session_factory.begin() as session:
        run = AiRun(user_id=user_id, kind="match")
        session.add(run)
    record: dict[str, Any] = {"run_id": str(run.run_id), "error": None, "items": []}
    try:
        report = await match_job(
            session_factory,
            user_id,
            run.run_id,
            fact_set_id=resume.fact_set_id,
            client=client,
            budget=ModelBudget(settings.model_max_attempts + 1, TOKEN_BUDGET_PER_PAIR),
            snapshot=job.snapshot,
            facts=resume.inputs.facts,
            criteria=resume.inputs.criteria,
            education=resume.inputs.education,
        )
        record["items"] = _serialize_items(report, job, resume)
    except (
        APIError,
        ValidationError,
        MatchValidationError,
        TruncatedOutputError,
        EmptyModelResponseError,
        BudgetExhaustedError,
    ) as error:
        record["error"] = type(error).__name__
    status = RunStatus.FAILED if record["error"] else RunStatus.SUCCEEDED
    record["calls"] = await _finish_run(session_factory, user_id, run.run_id, status)
    return record


async def _finish_run(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: UUID,
    run_id: UUID,
    status: RunStatus,
) -> list[dict[str, Any]]:
    """结束匹配运行并读取网关实际记录的调用次数和用量。"""
    async with session_factory.begin() as session:
        await set_run_status(session, user_id, run_id, status)
        calls = await session.scalars(
            select(ModelCall)
            .where(ModelCall.run_id == run_id)
            .order_by(ModelCall.created_at)
        )
        return [
            {
                "outcome": call.outcome,
                "model_name": call.model,
                "prompt_version": call.prompt_version,
                "prompt_tokens": call.prompt_tokens,
                "completion_tokens": call.completion_tokens,
                "latency_ms": call.latency_ms,
            }
            for call in calls
        ]


def _require_sealed_labels() -> str:
    """开发和测试两边都封存后才开始付费评测。"""
    for split in ("dev", "test"):
        location = labels_path(split)
        path = location.relative_to(REPO_ROOT)
        sealed = subprocess.run(
            ["git", "show", f"HEAD:{path}"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=False,
        )
        if sealed.returncode or sealed.stdout != location.read_bytes():
            raise ValueError(f"{path} 必须提交封存后才能运行。")
        load_labels(split)
    return subprocess.run(
        ["git", "log", "-1", "--format=%H", "--", str(labels_path("test"))],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()


def _output_path(split: Split, runs: int, output: Path | None) -> Path:
    if split == "test":
        if runs != OFFICIAL_TEST_RUNS or output is not None:
            raise ValueError("正式测试集固定为 3 次，使用默认输出路径。")
        return RESULT_DIR / "e3-test-official.json"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return output or RESULT_DIR / f"e3-dev-{stamp}.json"


def _write_output(path: Path, record: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


async def _run_pairs(
    session_factory: async_sessionmaker[AsyncSession],
    client: AsyncOpenAI,
    user_id: UUID,
    split: Split,
    path: Path,
    output: dict[str, Any],
) -> None:
    resume_ids, jd_ids = load_split(split)
    resumes = {name: load_resume(name, user_id) for name in resume_ids}
    jobs = {name: load_job(name) for name in jd_ids}
    # 独占创建保留正式轮的位置，每组结束后原子写回，保留中断前的结果。
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(output, stream, ensure_ascii=False, indent=2)
    for index in range(1, output["runs"] + 1):
        for resume_id, resume in resumes.items():
            for jd_id, job in jobs.items():
                result = await run_pair(session_factory, user_id, client, resume, job)
                pair = pair_id(resume_id, jd_id)
                output["attempts"].append(
                    {"run_index": index, "pair_id": pair, **result}
                )
                _write_output(path, output)
                print(f"第 {index} 次 {pair} {result['error'] or '完成'}", flush=True)


async def main(split: Split, runs: int, output_path: Path | None) -> int:
    path = _output_path(split, runs, output_path)
    if path.exists():
        raise FileExistsError(path)
    sealed_commit = _require_sealed_labels()
    api_key = settings.deepseek_api_key.get_secret_value()
    if not api_key:
        raise ValueError("未配置模型密钥。")
    output: dict[str, Any] = {
        "eval": "E3",
        "split": split,
        "created_at": datetime.now(UTC).isoformat(),
        "dataset": {**dataset_version(split), "test_labels_commit": sealed_commit},
        "git": git_state(),
        "prompt_version": load_prompt("match_job").label,
        "model": settings.model_for_purpose(ModelPurpose.MATCH).model_dump(),
        "parameters": {
            "json_output": True,
            "max_attempts": settings.model_max_attempts,
            "retry_delay_seconds": settings.model_retry_delay_seconds,
            "token_budget_per_pair": TOKEN_BUDGET_PER_PAIR,
        },
        "runs": runs,
        "attempts": [],
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
            await _run_pairs(session_factory, client, user_id, split, path, output)
    finally:
        await engine.dispose()
    print(f"结果已写入 {path}")
    return int(any(attempt["error"] for attempt in output["attempts"]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("dev", "test"), required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--output", type=Path, help="开发集的输出文件")
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs 必须大于 0")
    try:
        raise SystemExit(asyncio.run(main(args.split, args.runs, args.output)))
    except (ValueError, FileExistsError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"E3 无法运行：{error}\n")
