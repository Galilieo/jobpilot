"""运行 E5 改写对照。每组单独记账，逐组写入本地结果。"""

import argparse
import asyncio
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from fcntl import LOCK_EX, LOCK_NB, flock
from pathlib import Path
from time import monotonic
from typing import Annotated, Any
from uuid import UUID

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy.ext.asyncio import async_sessionmaker

from evals.runners.e1_extract_facts import git_state
from evals.runners.e3_dataset import Split
from evals.runners.e3_match import RESULT_DIR, _write_output
from evals.runners.e5_dataset import Pair, dataset_version, pairs_for
from evals.runners.e5_store import Factory, calls_of, remove_run_account, seed_run
from jobpilot.api.tailoring import _MAX_CONCURRENT_REQUESTS, _TAILOR_BUDGET
from jobpilot.config import REPO_ROOT, ModelPurpose, settings
from jobpilot.db.models import STOP_REASON_STATUS, CheckStatus, RunStatus, StopReason
from jobpilot.db.session import create_database_engine
from jobpilot.services.facts import TruncatedOutputError
from jobpilot.services.llm_gateway import (
    BudgetExhaustedError,
    ModelBudget,
    request_completion,
    wrap_untrusted_text,
)
from jobpilot.services.prompts import Prompt, load_prompt
from jobpilot.services.tailoring import MODEL_ERRORS, CheckLevel
from jobpilot.services.tailoring_graph import TailorDeps, TailorState, run_tailoring
from jobpilot.services.tailoring_store import stop_reason_of

PROMPT_DIR = REPO_ROOT / "evals" / "prompts"
PLACEHOLDER = "【待补】"


class Version(StrEnum):
    A = "A"
    A2 = "A2"
    B = "B"
    C = "C"
    D = "D"


CHECK_LEVEL = {
    Version.B: CheckLevel.NONE,
    Version.C: CheckLevel.PROGRAM,
    Version.D: CheckLevel.FULL,
}
NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class RawEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experience: NonBlank | None
    bullets: list[NonBlank] = Field(min_length=1)


class RawRewrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[RawEntry] = Field(min_length=1)


class EvalBullet(BaseModel):
    experience: str | None
    text: str
    fact_ids: tuple[UUID, ...] = ()
    check_status: CheckStatus | None = None


class Attempt(BaseModel):
    pair_id: str
    run_id: UUID
    status: RunStatus
    error: str | None = None
    bullets: list[EvalBullet] = Field(default_factory=list)
    wall_seconds: float
    calls: list[dict[str, Any]]


def eval_prompt(name: str) -> Prompt:
    header, separator, body = (
        (PROMPT_DIR / f"{name}.txt").read_text(encoding="utf-8").partition("\n---\n")
    )
    if not separator or not header.startswith("version: ") or not body.strip():
        raise ValueError(f"提示词文件缺少版本头或正文：{name}")
    version = int(header.removeprefix("version: "))
    if version < 1:
        raise ValueError(f"提示词版本必须大于 0：{name}")
    return Prompt(name, version, body.strip())


def _baseline_messages(pair: Pair, prompt: str) -> list[ChatCompletionMessageParam]:
    content = "\n\n".join(
        [
            wrap_untrusted_text("resume", pair.resume_text),
            wrap_untrusted_text("job", pair.jd_text),
        ]
    )
    return [{"role": "system", "content": prompt}, {"role": "user", "content": content}]


def _parse_baseline(completion: ChatCompletion) -> RawRewrite:
    choice = completion.choices[0]
    if choice.finish_reason == "length":
        raise TruncatedOutputError("整份简历改写输出被截断")
    return RawRewrite.model_validate_json(choice.message.content or "")


async def _baseline(
    factory: Factory,
    client: AsyncOpenAI,
    user_id: UUID,
    run_id: UUID,
    pair: Pair,
    version: Version,
) -> list[EvalBullet]:
    prompts = [eval_prompt("e5_a")]
    if version is Version.A2:
        prompts.append(eval_prompt("e5_a2"))
    completion = await request_completion(
        factory,
        user_id,
        run_id,
        client=client,
        purpose=ModelPurpose.REWRITE,
        prompt_version="+".join(prompt.label for prompt in prompts),
        messages=_baseline_messages(
            pair, "\n\n".join(prompt.text for prompt in prompts)
        ),
        json_output=True,
        # 对照定义为一次实际请求；格式错误或上游失败不触发第二次请求。
        budget=ModelBudget(
            remaining_calls=1, remaining_tokens=_TAILOR_BUDGET["remaining_tokens"]
        ),
    )
    output = _parse_baseline(completion)
    return [
        EvalBullet(experience=entry.experience, text=text)
        for entry in output.entries
        for text in entry.bullets
    ]


def _product_bullets(state: TailorState) -> list[EvalBullet]:
    outcomes = {outcome.key: outcome for outcome in state["outcomes"]}
    return [
        EvalBullet(
            experience=block.experience,
            text=bullet.text,
            fact_ids=bullet.fact_ids,
            check_status=bullet.check_status,
        )
        for block in state.get("plan", ())
        if block.key in outcomes
        for bullet in outcomes[block.key].bullets
    ]


async def _run_pair(
    factory: Factory,
    client: AsyncOpenAI,
    pair: Pair,
    version: Version,
) -> Attempt:
    level = CHECK_LEVEL.get(version)
    user_id, run_id = await seed_run(factory, pair, product=level is not None)
    started = monotonic()
    bullets, status, error = [], RunStatus.SUCCEEDED, None
    try:
        if level is None:
            bullets = await _baseline(factory, client, user_id, run_id, pair, version)
        else:
            deps = TailorDeps(
                factory,
                client,
                ModelBudget(**_TAILOR_BUDGET),
                asyncio.Semaphore(_MAX_CONCURRENT_REQUESTS),
                check_level=level,
            )
            state = await run_tailoring(deps, user_id, run_id)
            reason = state.get("stop_reason") or stop_reason_of(state["outcomes"])
            if reason:
                status, error = STOP_REASON_STATUS[reason], reason.value
            bullets = _product_bullets(state)
    except BudgetExhaustedError:
        status, error = RunStatus.BUDGET_EXHAUSTED, StopReason.BUDGET_EXHAUSTED.value
    except MODEL_ERRORS as exc:
        status, error = RunStatus.FAILED, type(exc).__name__
    finally:
        wall_seconds = monotonic() - started
        calls = await calls_of(factory, user_id)
        await remove_run_account(factory, user_id)

    return Attempt(
        pair_id=pair.key,
        run_id=run_id,
        status=status,
        error=error,
        bullets=bullets,
        wall_seconds=wall_seconds,
        calls=calls,
    )


def _require_e3_final() -> None:
    notes = (REPO_ROOT / "evals" / "README.md").read_text(encoding="utf-8")
    e3_path = RESULT_DIR / "e3-test-official.json"
    if "匹配提示词定稿" not in notes or not e3_path.is_file():
        raise ValueError("E5 测试集须等 E3 正式测试结束且 README 记录匹配提示词定稿")
    e3 = json.loads(e3_path.read_text(encoding="utf-8"))
    if e3.get("prompt_version") != load_prompt("match_job").label or len(
        e3.get("attempts", ())
    ) != 3 * len(pairs_for("test")):
        raise ValueError("当前匹配提示词与已完成的 E3 正式测试不一致")


def _run_metadata(version: Version, split: Split) -> dict[str, Any]:
    purposes = (
        (ModelPurpose.REWRITE,)
        if version not in CHECK_LEVEL
        else (ModelPurpose.MATCH, ModelPurpose.REWRITE, ModelPurpose.GROUND_CHECK)
    )
    common = settings.rewrite
    models = {}
    for purpose in purposes:
        configured = settings.model_for_purpose(purpose)
        if (configured.model_name, configured.temperature, configured.thinking) != (
            common.model_name,
            common.temperature,
            common.thinking,
        ):
            raise ValueError("E5 要求匹配、改写和第三层使用相同模型、温度及思考模式")
        models[purpose.value] = configured.model_dump()
    prompts = (
        [
            load_prompt(name)
            for name in (
                "match_job",
                "rewrite_bullets",
                "revise_bullet",
                "ground_check",
                "repair_json",
            )
        ]
        if version in CHECK_LEVEL
        else [eval_prompt("e5_a")]
    )
    if version is Version.A2:
        prompts.append(eval_prompt("e5_a2"))
    return dict(
        eval="E5",
        version=version,
        split=split,
        git=git_state(),
        dataset=dataset_version(split),
        created_at=datetime.now(UTC).isoformat(),
        models=models,
        prompts={prompt.name: prompt.label for prompt in prompts},
        parameters=dict(
            budget=_TAILOR_BUDGET
            if version in CHECK_LEVEL
            else {
                **_TAILOR_BUDGET,
                "remaining_calls": 1,
            },
            max_concurrent_requests=_MAX_CONCURRENT_REQUESTS
            if version in CHECK_LEVEL
            else 1,
            json_output=True,
            gateway_max_attempts=settings.model_max_attempts,
            retry_delay_seconds=settings.model_retry_delay_seconds,
        ),
        attempts=[],
    )


@contextmanager
def evaluation_lock():
    """各版本串行运行，避免同一固定事实 ID 同时插入不同运行。"""
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    with (RESULT_DIR / ".e5.lock").open("a") as lock:
        try:
            flock(lock, LOCK_EX | LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("已有 E5 批次正在运行") from exc
        yield


async def _run_batch(
    path: Path,
    pairs: list[Pair],
    version: Version,
    output: dict[str, Any],
) -> None:
    api_key = settings.deepseek_api_key.get_secret_value()
    if not api_key:
        raise ValueError("未配置模型密钥")
    engine = create_database_engine(
        settings.database_url,
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        with path.open("x", encoding="utf-8") as stream:
            json.dump(output, stream, ensure_ascii=False, indent=2)
        async with AsyncOpenAI(
            api_key=api_key, base_url=settings.deepseek_base_url
        ) as client:
            for pair in pairs:
                attempt = await _run_pair(factory, client, pair, version)
                output["attempts"].append(attempt.model_dump(mode="json"))
                _write_output(path, output)
                print(
                    f"{version} {pair.key} {attempt.status} "
                    f"({len(attempt.bullets)} 条)",
                    flush=True,
                )
    finally:
        await engine.dispose()


async def main(
    version: Version,
    split: Split,
    selected: list[str] | None,
    output_path: Path | None,
) -> None:
    if split == "test":
        if selected is not None or output_path is not None:
            raise ValueError("正式测试集每个版本只跑一次，使用完整配对与固定输出")
        _require_e3_final()
        path = RESULT_DIR / f"e5-test-{version}-official.json"
    else:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        path = output_path or RESULT_DIR / f"e5-dev-{version}-{stamp}.json"
    pairs = pairs_for(split, selected)
    record = _run_metadata(version, split)
    record["planned_pairs"] = [pair.key for pair in pairs]
    with evaluation_lock():
        await _run_batch(path, pairs, version, record)
    print(f"结果已写入 {path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", type=Version, choices=list(Version), required=True)
    parser.add_argument("--split", choices=("dev", "test"), required=True)
    parser.add_argument("--pairs", nargs="+", help="只运行指定开发集配对")
    parser.add_argument("--output", type=Path, help="只用于开发集")
    args = parser.parse_args()
    try:
        asyncio.run(main(args.version, args.split, args.pairs, args.output))
    except (ValueError, FileExistsError) as error:
        parser.exit(1, f"{error}\n")
