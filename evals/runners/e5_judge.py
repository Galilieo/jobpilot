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
from evals.runners.w_budget import evaluation_http_client
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


class EvidenceBasis(StrEnum):
    CONFIRMED = "confirmed"
    RESUME = "resume"

    @property
    def label(self) -> str:
        return "已确认事实" if self is EvidenceBasis.CONFIRMED else "简历原文"


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
    archived_evidence: str | None = None

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
    by_version = {Version(row["version"]): row for row in records}
    if by_version[Version.B]["attempts"] and by_version[Version.B]["attempts"][0].get(
        "shared_draft"
    ):
        source = next(
            path
            for path, row in zip(paths, records, strict=True)
            if row["version"] == Version.B
        )
        expected_source = {
            "file": source.name,
            "sha256": sha256(source.read_bytes()).hexdigest(),
        }
        if any(
            by_version[version].get("shared_from") != expected_source
            for version in (Version.C, Version.D)
        ):
            raise ValueError("C、D 必须使用所提供 B 结果的同一初稿")
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


def _messages(
    batch: JudgeBatch, prompt: Prompt, basis: EvidenceBasis
) -> list[ChatCompletionMessageParam]:
    candidates = [
        dict(id=item.index, experience=item.bullet.experience, text=item.bullet.text)
        for item in batch.candidates
    ]
    evidence = batch.archived_evidence
    if evidence is None:
        evidence = _evidence_text(batch.pair, basis)
    content = "\n\n".join(
        [
            f"证据口径：{basis.label}。只根据下面的证据判定。",
            wrap_untrusted_text("evidence", evidence),
            wrap_untrusted_text(
                "candidates", json.dumps(candidates, ensure_ascii=False)
            ),
        ]
    )
    return [
        {"role": "system", "content": prompt.text},
        {"role": "user", "content": content},
    ]


def _evidence_text(pair: Pair, basis: EvidenceBasis) -> str:
    if basis is EvidenceBasis.RESUME:
        return pair.resume_text
    return json.dumps(confirmed_evidence(pair), ensure_ascii=False)


async def _judge_batch(
    factory: Factory,
    client: AsyncOpenAI,
    log: JudgeTransportLog,
    batch: JudgeBatch,
    prompt: Prompt,
    basis: EvidenceBasis,
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
            messages=_messages(batch, prompt, basis),
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
    basis: EvidenceBasis,
) -> Path:
    if split == "dev":
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        return override or RESULT_DIR / f"e5-judge-dev-{basis}-{stamp}.json"
    if override is not None or calibration is None:
        raise ValueError("测试集只判一次，使用固定输出并提供独立验证记录")
    outcome = json.loads(calibration.read_text(encoding="utf-8"))
    if (
        outcome.get("count", 0) < 40
        or outcome.get("source") != "generated_dev"
        or outcome.get("evidence_basis") != basis
        or outcome.get("judge_prompt") != prompt.label
        or outcome.get("judge_prompt_sha256")
        != sha256(prompt.text.encode()).hexdigest()
        or outcome.get("judge_model") != model
    ):
        raise ValueError("正式裁判的口径、模型和提示词须与独立验证一致")
    return RESULT_DIR / f"e5-judge-test-{basis}-official.json"


def _challenge_batches(path: Path) -> list[JudgeBatch]:
    from evals.runners.e5_validation import candidates

    groups: dict[str, list[Candidate]] = {}
    for candidate in candidates(path):
        groups.setdefault(candidate.pair.resume_id, []).append(candidate)
    return [
        JudgeBatch(group[0].version, group[0].pair, group)
        for items in groups.values()
        for group in batched(items, JUDGE_BATCH_SIZE)
    ]


def _archived_batches(archive: dict, sample: dict) -> list[JudgeBatch]:
    if archive.get("split") != "dev" or any(
        archive[field] != sample[field] for field in ("inputs", "dataset")
    ):
        raise ValueError("旧裁判输出与原盲标键表不一致")
    keys = set(sample["keys"])
    pairs = {pair.key: pair for pair in pairs_for("dev")}
    batches = [
        _archived_batch(row, keys, pairs[row["pair_id"]])
        for row in archive["batches"]
        if keys.intersection(row["keys"])
    ]
    if {item.key for batch in batches for item in batch.candidates} != keys:
        raise ValueError("旧裁判记录缺少盲标条目")
    return batches


def _archived_block(content: str, label: str) -> str:
    _, sep, block = content.partition(f'<untrusted_data label="{label}">\n')
    body, end, _ = block.partition("\n</untrusted_data>")
    if not (sep and end):
        raise ValueError(f"旧裁判请求缺少 {label} 区块")
    return body


def _archived_batch(row: dict, keys: set[str], pair: Pair) -> JudgeBatch:
    content = row["requests"][0]["messages"][1]["content"]
    version = Version(row["version"])
    candidates = tuple(
        Candidate(
            version,
            pair,
            item["id"],
            EvalBullet(experience=item["experience"], text=item["text"]),
        )
        for item in json.loads(_archived_block(content, "candidates"))
        if f"{version}/{pair.key}/{item['id']}" in keys
    )
    return JudgeBatch(
        version,
        pair,
        candidates,
        archived_evidence=_archived_block(content, "evidence"),
    )


async def main(
    paths: list[Path],
    output: Path | None,
    model: str,
    sample_map: Path | None,
    calibration: Path | None,
    basis: EvidenceBasis = EvidenceBasis.CONFIRMED,
    challenge: Path | None = None,
    archived_judge: Path | None = None,
) -> None:
    prompt = eval_prompt("e5_judge")
    if archived_judge:
        if (
            paths
            or challenge
            or calibration
            or not sample_map
            or basis is not EvidenceBasis.CONFIRMED
        ):
            raise ValueError("旧盲标回归只用原裁判存档和盲标键表，按已确认事实口径")
        archive = json.loads(archived_judge.read_text(encoding="utf-8"))
        sample = json.loads(sample_map.read_text(encoding="utf-8"))
        split, batches = "dev", _archived_batches(archive, sample)
    elif challenge:
        if paths or sample_map or calibration:
            raise ValueError("独立挑战单独运行")
        paths, split, batches = [challenge], "dev", _challenge_batches(challenge)
    else:
        split, records = read_outputs(paths)
        if split == "test" and sample_map is not None:
            raise ValueError("正式测试必须判完所有条目")
        keys = (
            set(json.loads(sample_map.read_text(encoding="utf-8"))["keys"])
            if sample_map
            else None
        )
        batches = _batches(split, records, keys)
    path = _judging_path(split, output, calibration, prompt, model, basis)
    metadata = _metadata(paths, split, batches, prompt, basis)
    if archived_judge:
        metadata.update(
            dataset=archive["dataset"],
            inputs=archive["inputs"],
            archived_input=input_digests([archived_judge, sample_map]),
        )
    if calibration:
        metadata["calibration"] = json.loads(calibration.read_text(encoding="utf-8"))
    with evaluation_lock():
        await _judge_all(path, metadata, batches, prompt, model, basis)
    print(f"裁判结果已写入 {path}")


def _metadata(paths, split, batches, prompt, basis):
    return dict(
        eval="E5-judge",
        split=split,
        dataset=dataset_version(split),
        inputs=[source.name for source in paths],
        input_digests=input_digests(paths),
        planned_batches=len(batches),
        evidence_basis=basis,
        judge_prompt=prompt.label,
        judge_prompt_sha256=sha256(prompt.text.encode()).hexdigest(),
        repair_prompt=load_prompt("repair_json").label,
        git=git_state(),
        batches=[],
    )


async def _judge_all(
    path: Path,
    output: dict[str, Any],
    batches: list[JudgeBatch],
    prompt: Prompt,
    model: str,
    basis: EvidenceBasis,
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
        http_client, batch_budget = evaluation_http_client(log.request, log.response)
        async with AsyncOpenAI(
            api_key=api_key,
            base_url=settings.deepseek_base_url,
            http_client=http_client,
        ) as client:
            with path.open("x", encoding="utf-8") as stream:
                json.dump(output, stream, ensure_ascii=False, indent=2)
            for index, batch in enumerate(batches, start=1):
                result = await _judge_batch(factory, client, log, batch, prompt, basis)
                output["batches"].append(result)
                _write_output(path, output)
                if batch_budget is not None:
                    batch_budget.check()
                print(
                    f"裁判 {index}/{len(batches)} {result['error'] or '完成'}",
                    flush=True,
                )
    finally:
        settings.ground_check = original_settings
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", type=Path, nargs="*")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--model", default=JUDGE_MODEL)
    parser.add_argument("--sample-map", type=Path, help="开发集仅重判指定校准样本")
    parser.add_argument(
        "--calibration", type=Path, help="正式测试使用的同口径独立验证记录"
    )
    parser.add_argument(
        "--basis",
        type=EvidenceBasis,
        choices=list(EvidenceBasis),
        default=EvidenceBasis.CONFIRMED,
    )
    parser.add_argument("--challenge", type=Path, help="独立裁判挑战样本")
    parser.add_argument("--archived-judge", type=Path, help="从旧裁判请求重判原盲标")
    args = parser.parse_args()
    try:
        asyncio.run(
            main(
                args.inputs,
                args.output,
                args.model,
                args.sample_map,
                args.calibration,
                args.basis,
                args.challenge,
                args.archived_judge,
            )
        )
    except (ValueError, FileExistsError) as error:
        parser.exit(1, f"{error}\n")
