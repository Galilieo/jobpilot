"""逐条盲判 E5 改写，按组记录经过网关的裁判请求与原始回复。"""

import argparse
import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from itertools import batched
from pathlib import Path
from time import monotonic
from typing import Any

import httpx
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator
from sqlalchemy.ext.asyncio import async_sessionmaker

from evals.runners.e1_extract_facts import git_state
from evals.runners.e3_match import RESULT_DIR, _write_output
from evals.runners.e5_dataset import (
    Pair,
    confirmed_evidence,
    dataset_version,
    pairs_for,
)
from evals.runners.e5_run import (
    Attempt,
    EvalBullet,
    Version,
    eval_prompt,
    evaluation_lock,
)
from evals.runners.e5_store import Factory, calls_of, remove_run_account, seed_run
from jobpilot.config import ModelPurpose, settings
from jobpilot.db.models import CheckStatus
from jobpilot.db.session import create_database_engine
from jobpilot.services.facts import TruncatedOutputError
from jobpilot.services.llm_gateway import (
    BudgetExhaustedError,
    ModelBudget,
    request_completion,
    wrap_untrusted_text,
)
from jobpilot.services.prompts import Prompt, load_prompt
from jobpilot.services.tailoring import MODEL_ERRORS

JUDGE_MODEL = "deepseek-v4-pro"
JUDGE_BATCH_SIZE = 8
JUDGE_TOKEN_BUDGET = 200_000


class Fabrication(StrEnum):
    ADDITION = "addition"
    NUMBER = "number"
    ROLE = "role"
    SKILL_LEVEL = "skill_level"
    CROSS_EXPERIENCE = "cross_experience"
    BASIC_INFO = "basic_info"
    SCOPE = "scope"


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    types: list[Fabrication]
    reason: str = Field(min_length=1)

    @field_validator("types")
    @classmethod
    def unique_types(cls, types: list[Fabrication]) -> list[Fabrication]:
        if len(types) != len(set(types)):
            raise ValueError("编造类别重复")
        return types


class JudgeReply(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdicts: list[Verdict]


@dataclass(frozen=True)
class Candidate:
    version: Version
    pair: Pair
    index: int
    bullet: EvalBullet

    @property
    def key(self) -> str:
        return f"{self.version}/{self.pair.key}/{self.index}"


@dataclass
class JudgeBatch:
    version: Version
    pair: Pair
    candidates: tuple[Candidate, ...]
    requests: list[dict[str, Any]] = field(default_factory=list)

    def parse(self, completion: ChatCompletion) -> JudgeReply:
        choice = completion.choices[0]
        if choice.finish_reason == "length":
            raise TruncatedOutputError("裁判输出被截断")
        reply = JudgeReply.model_validate_json(choice.message.content or "")
        # 用 Pydantic 的错误进入网关的 JSON 修复流程，以便修复漏判或重复的 id。
        JudgeCoverage(
            ids=[verdict.id for verdict in reply.verdicts],
            expected=tuple(item.index for item in self.candidates),
        )
        return reply


class JudgeCoverage(BaseModel):
    expected: tuple[int, ...]
    ids: list[int]

    @field_validator("ids")
    @classmethod
    def exact_coverage(cls, ids: list[int], info: ValidationInfo) -> list[int]:
        if sorted(ids) != sorted(info.data["expected"]):
            raise ValueError("每个候选 id 必须恰好判一次")
        return ids


@dataclass
class JudgeTransportLog:
    """只捕获合成裁判消息与模型返回的 choices，不保存认证信息。"""

    current: JudgeBatch | None = None

    async def request(self, request: httpx.Request) -> None:
        if self.current is not None:
            payload = json.loads(request.content)
            self.current.requests.append(
                {"messages": payload["messages"], "response": None}
            )

    async def response(self, response: httpx.Response) -> None:
        if self.current is None:
            return
        await response.aread()
        output: dict[str, Any] = {"http_status": response.status_code}
        if response.is_success:
            try:
                payload = response.json()
            except ValueError:
                output["body"] = response.text
            else:
                output["choices"] = payload.get("choices", [])
        self.current.requests[-1]["response"] = output


def input_digests(paths: list[Path]) -> dict[str, str]:
    return {path.name: sha256(path.read_bytes()).hexdigest() for path in paths}


def read_outputs(paths: list[Path]) -> tuple[str, list[dict[str, Any]]]:
    records = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    splits = {row["split"] for row in records}
    if len(splits) != 1 or {row["version"] for row in records} != set(Version):
        raise ValueError("请提供同一分组的 A、A2、B、C、D 五个结果文件，各一份")
    if len(records) != len(Version) or len({path.name for path in paths}) != len(
        Version
    ):
        raise ValueError("每个版本须恰好提供一个结果文件")
    split = splits.pop()
    if split not in ("test", "dev"):
        raise ValueError("未知数据分组")
    expected = dataset_version(split)
    plan = records[0]["planned_pairs"]
    if any(
        row["dataset"] != expected
        or row["planned_pairs"] != plan
        or [attempt["pair_id"] for attempt in row["attempts"]] != plan
        for row in records
    ):
        raise ValueError("数据指纹或配对清单不一致，或批次未完成")
    if split == "test" and plan != [pair.key for pair in pairs_for("test")]:
        raise ValueError("正式测试必须包含完整测试集")
    return split, records


def _batches(
    split: str,
    records: list[dict[str, Any]],
    keys: set[str] | None,
) -> list[JudgeBatch]:
    pairs = {pair.key: pair for pair in pairs_for(split)}
    batches = []
    found: set[str] = set()
    for record in records:
        version = Version(record["version"])
        for raw in record["attempts"]:
            attempt = Attempt.model_validate(raw)
            pair = pairs[attempt.pair_id]
            items = (
                Candidate(version, pair, index, bullet)
                for index, bullet in enumerate(attempt.bullets, start=1)
                if bullet.check_status is not CheckStatus.PENDING
            )
            selected = [item for item in items if keys is None or item.key in keys]
            found.update(item.key for item in selected)
            batches.extend(
                JudgeBatch(version, pair, group)
                for group in batched(selected, JUDGE_BATCH_SIZE)
            )
    if keys is not None and found != keys:
        raise ValueError("找不到全部校准条目，需使用生成校准表时的原批次")
    return batches


def _messages(batch: JudgeBatch, prompt: Prompt) -> list[ChatCompletionMessageParam]:
    candidates = [
        dict(id=item.index, experience=item.bullet.experience, text=item.bullet.text)
        for item in batch.candidates
    ]
    content = "\n\n".join(
        [
            wrap_untrusted_text(
                "evidence",
                json.dumps(confirmed_evidence(batch.pair), ensure_ascii=False),
            ),
            wrap_untrusted_text(
                "candidates", json.dumps(candidates, ensure_ascii=False)
            ),
        ]
    )
    return [
        {"role": "system", "content": prompt.text},
        {"role": "user", "content": content},
    ]


async def _judge_batch(
    factory: Factory,
    client: AsyncOpenAI,
    log: JudgeTransportLog,
    batch: JudgeBatch,
    prompt: Prompt,
) -> dict[str, Any]:
    user_id, run_id = await seed_run(factory, batch.pair, product=False)
    started = monotonic()
    log.current = batch
    verdicts = []
    error_name = None
    try:
        completion = await request_completion(
            factory,
            user_id,
            run_id,
            client=client,
            purpose=ModelPurpose.GROUND_CHECK,
            prompt_version=prompt.label,
            messages=_messages(batch, prompt),
            json_output=True,
            validate=batch.parse,
            budget=ModelBudget(settings.model_max_attempts + 1, JUDGE_TOKEN_BUDGET),
        )
        verdicts = [
            verdict.model_dump(mode="json")
            for verdict in batch.parse(completion).verdicts
        ]
    except (*MODEL_ERRORS, BudgetExhaustedError) as error:
        error_name = type(error).__name__
    finally:
        log.current = None
        wall_seconds = monotonic() - started
        calls = await calls_of(factory, user_id)
        await remove_run_account(factory, user_id)
    return dict(
        version=batch.version,
        pair_id=batch.pair.key,
        keys=[item.key for item in batch.candidates],
        verdicts=verdicts,
        error=error_name,
        requests=batch.requests,
        wall_seconds=wall_seconds,
        calls=calls,
    )


def _judging_path(
    split: str,
    override: Path | None,
    calibration: Path | None,
    prompt: Prompt,
    model: str,
) -> Path:
    if split == "dev":
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        return override or RESULT_DIR / f"e5-judge-dev-{stamp}.json"
    if override is not None or calibration is None:
        raise ValueError("测试集只判一次，使用固定输出并提供通过的校准 JSON")
    outcome = json.loads(calibration.read_text(encoding="utf-8"))
    if (
        outcome.get("kappa", 0) < 0.6
        or outcome.get("count", 0) < 40
        or outcome.get("judge_prompt") != prompt.label
        or outcome.get("judge_prompt_sha256")
        != sha256(prompt.text.encode()).hexdigest()
        or outcome.get("judge_model") != model
    ):
        raise ValueError("正式裁判的模型和提示词须与已通过的校准一致")
    return RESULT_DIR / "e5-judge-test-official.json"


async def main(
    paths: list[Path],
    output: Path | None,
    model: str,
    sample_map: Path | None,
    calibration: Path | None,
) -> None:
    split, records = read_outputs(paths)
    prompt = eval_prompt("e5_judge")
    path = _judging_path(split, output, calibration, prompt, model)
    if split == "test" and sample_map is not None:
        raise ValueError("正式测试必须判完所有条目")
    keys = (
        set(json.loads(sample_map.read_text(encoding="utf-8"))["keys"])
        if sample_map
        else None
    )
    batches = _batches(split, records, keys)
    metadata = dict(
        eval="E5-judge",
        split=split,
        dataset=records[0]["dataset"],
        inputs=[source.name for source in paths],
        input_digests=input_digests(paths),
        planned_batches=len(batches),
        judge_prompt=prompt.label,
        judge_prompt_sha256=sha256(prompt.text.encode()).hexdigest(),
        repair_prompt=load_prompt("repair_json").label,
        git=git_state(),
        batches=[],
    )
    if calibration:
        metadata["calibration"] = json.loads(calibration.read_text(encoding="utf-8"))
    with evaluation_lock():
        await _judge_all(path, metadata, batches, prompt, model)
    print(f"裁判结果已写入 {path}")


async def _judge_all(
    path: Path,
    output: dict[str, Any],
    batches: list[JudgeBatch],
    prompt: Prompt,
    model: str,
) -> None:
    api_key = settings.deepseek_api_key.get_secret_value()
    if not api_key:
        raise ValueError("未配置模型密钥")
    original_settings = settings.ground_check
    # 只在独立的裁判进程替换该用途的模型，产品使用原有设置。
    settings.ground_check = original_settings.model_copy(
        update={"model_name": model, "temperature": 0, "thinking": False}
    )
    output["model"] = settings.ground_check.model_dump()
    engine = create_database_engine(
        settings.database_url,
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    log = JudgeTransportLog()
    try:
        with path.open("x", encoding="utf-8") as stream:
            json.dump(output, stream, ensure_ascii=False, indent=2)
        async with AsyncOpenAI(
            api_key=api_key,
            base_url=settings.deepseek_base_url,
            http_client=httpx.AsyncClient(
                event_hooks={
                    "request": [log.request],
                    "response": [log.response],
                }
            ),
        ) as client:
            for index, batch in enumerate(batches, start=1):
                result = await _judge_batch(factory, client, log, batch, prompt)
                output["batches"].append(result)
                _write_output(path, output)
                print(
                    f"裁判 {index}/{len(batches)} {result['error'] or '完成'}",
                    flush=True,
                )
    finally:
        settings.ground_check = original_settings
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", type=Path, nargs=5)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--model", default=JUDGE_MODEL)
    parser.add_argument("--sample-map", type=Path, help="开发集仅重判指定校准样本")
    parser.add_argument("--calibration", type=Path, help="正式测试使用的通过校准记录")
    args = parser.parse_args()
    try:
        asyncio.run(
            main(
                args.inputs, args.output, args.model, args.sample_map, args.calibration
            )
        )
    except (ValueError, FileExistsError) as error:
        parser.exit(1, f"{error}\n")
