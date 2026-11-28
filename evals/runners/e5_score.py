"""生成 E5 盲标表、计算裁判一致性和五组对照指标。"""

import argparse
import csv
import json
import re
from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from random import Random
from typing import Any

from evals.runners.e3_dataset import DATASETS
from evals.runners.e3_match import RESULT_DIR
from evals.runners.e5_judge import (
    Candidate,
    EvidenceBasis,
    Verdict,
    _batches,
    input_digests,
    read_outputs,
)
from evals.runners.e5_run import PLACEHOLDER, Attempt, Version
from jobpilot.config import REPO_ROOT
from jobpilot.db.models import CheckStatus, RunStatus

CALIBRATION_SHEET = DATASETS / "e5" / "calibration-sheet.csv"
CALIBRATION_MAP = RESULT_DIR / "e5-calibration-map.json"
HUMAN_LABELS = REPO_ROOT / "evals" / "private" / "e5-calibration.csv"
PER_VERSION = 8
SAMPLE_SEED = 20261126
COLUMNS = ("改写句子", "相关原文", "有编造 / 没有")
HUMAN_VALUES = {"有编造": True, "没有": False}


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _save_json(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def _verdicts(judging: dict[str, Any]) -> dict[str, Verdict]:
    verdicts = {}
    for batch in judging["batches"]:
        expected = {int(key.rsplit("/", 1)[1]): key for key in batch["keys"]}
        received = [Verdict.model_validate(row) for row in batch["verdicts"]]
        if batch["error"] or sorted(expected) != sorted(row.id for row in received):
            raise ValueError("裁判有未完成批次或缺少判定，无法计分")
        for verdict in received:
            key = expected[verdict.id]
            if key in verdicts:
                raise ValueError(f"裁判结果重复：{key}")
            verdicts[key] = verdict
    if len(judging["batches"]) != judging["planned_batches"]:
        raise ValueError("裁判批次尚未完成")
    return verdicts


def _candidates(
    inputs: list[Path],
    judging: dict[str, Any],
) -> tuple[str, list[dict[str, Any]], list[Candidate]]:
    split, records = read_outputs(inputs)
    if judging["dataset"] != records[0]["dataset"] or judging["split"] != split:
        raise ValueError("裁判与改写数据不一致")
    if set(judging["inputs"]) != {path.name for path in inputs}:
        raise ValueError("裁判使用的是另一批改写文件")
    if "input_digests" in judging and judging["input_digests"] != input_digests(inputs):
        raise ValueError("改写结果文件在裁判之后发生变化")
    candidates = [
        candidate
        for batch in _batches(split, records, None)
        for candidate in batch.candidates
    ]
    return split, records, candidates


def _take_distinct(
    pool: list[Candidate],
    count: int,
    seen: set[tuple[str, str | None, str]],
) -> list[Candidate]:
    """优先抽不同的句子，需要时用重复句补足版本配额。"""
    distinct, repeated = [], []
    for item in pool:
        if len(distinct) == count:
            break
        signature = (item.pair.resume_id, item.bullet.experience, item.bullet.text)
        if signature in seen:
            repeated.append(item)
        else:
            distinct.append(item)
            seen.add(signature)
    return distinct + repeated[: count - len(distinct)]


def _draw(
    candidates: list[Candidate],
    verdicts: dict[str, Verdict],
) -> tuple[list[Candidate], dict[str, dict[str, int]]]:
    random = Random(SAMPLE_SEED)
    selected = []
    strata = {}
    seen: set[tuple[str, str | None, str]] = set()
    if {bool(verdict.types) for verdict in verdicts.values()} != {True, False}:
        raise ValueError("开发集裁判结果须同时包含有编造与无编造条目")
    for version in Version:
        pool = [item for item in candidates if item.version is version]
        if len(pool) < PER_VERSION:
            raise ValueError(f"{version} 不足 {PER_VERSION} 条")
        negative = [item for item in pool if not verdicts[item.key].types]
        positive = [item for item in pool if verdicts[item.key].types]
        random.shuffle(negative)
        random.shuffle(positive)
        n_positive = min(
            len(positive), max(PER_VERSION // 2, PER_VERSION - len(negative))
        )
        n_negative = PER_VERSION - n_positive
        selected.extend(_take_distinct(positive, n_positive, seen))
        selected.extend(_take_distinct(negative, n_negative, seen))
        strata[version.value] = {
            "provisional_positive": n_positive,
            "provisional_negative": n_negative,
        }
    random.shuffle(selected)
    return selected, strata


def _relevant_quotes(item: Candidate) -> str:
    """有人称简称或跨经历挪用时也展示最相关的补充原文。"""
    sentence = item.bullet.text
    experience = item.bullet.experience
    annotation = item.pair.annotation
    by_experience: dict[str, list[str]] = {}
    for row in annotation["facts"]:
        by_experience.setdefault(row["experience"], []).append(row["source_quote"])
    for row in annotation["education"]:
        by_experience.setdefault("教育", []).append(row["source_quote"])
    for row in annotation["criteria"].values():
        if row:
            by_experience.setdefault("求职条件", []).append(row["source_quote"])
    sentence_terms = _terms(sentence + (experience or ""))
    scored = []
    for title, quotes in by_experience.items():
        same = experience is not None and (experience in title or title in experience)
        for quote in dict.fromkeys(quotes):
            overlap = len(sentence_terms & _terms(title + quote))
            scored.append((same, overlap, title, quote))
    scored.sort(key=lambda row: row[:2], reverse=True)
    display: dict[str, list[str]] = {}
    for _, _, title, quote in scored[:8]:
        display.setdefault(title, []).append(quote)
    return "\n\n".join(
        f"【{title}】\n" + "\n".join(quotes) for title, quotes in display.items()
    )


def _terms(text: str) -> set[str]:
    compact = re.sub(r"\W+", "", text.casefold())
    return {compact[index : index + 2] for index in range(len(compact) - 1)}


def _row_hash(row: tuple[str, str]) -> str:
    return sha256(json.dumps(row, ensure_ascii=False).encode()).hexdigest()


def prepare(
    inputs: list[Path],
    judging_path: Path,
    sheet: Path,
    sample_map: Path,
) -> None:
    judging = _read_json(judging_path)
    split, _, candidates = _candidates(inputs, judging)
    if split != "dev":
        raise ValueError("只从开发集生成裁判校准表")
    verdicts = _verdicts(judging)
    if verdicts.keys() != {item.key for item in candidates}:
        raise ValueError("首次抽样需要已判完的开发集结果")
    selected, strata = _draw(candidates, verdicts)
    rows = [
        (
            f"【{item.bullet.experience or '技能概述'}】{item.bullet.text}",
            _relevant_quotes(item),
        )
        for item in selected
    ]
    if sheet.exists() or sample_map.exists():
        raise FileExistsError("校准表或盲标键表已存在")
    sheet.parent.mkdir(parents=True, exist_ok=True)
    with sheet.open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(COLUMNS)
        writer.writerows((*row, "") for row in rows)
    _save_json(
        sample_map,
        dict(
            keys=[item.key for item in selected],
            row_hashes=[_row_hash(row) for row in rows],
            strata=strata,
            seed=SAMPLE_SEED,
            dataset=judging["dataset"],
            inputs=judging["inputs"],
        ),
    )
    print(f"已生成 {len(rows)} 条盲标材料：{sheet}")


def agreement(human: list[bool], judge: list[bool]) -> tuple[float, float]:
    count = len(human)
    if not count or count != len(judge):
        raise ValueError("人工与裁判标签数须一致且非空")
    observed = (
        sum(left == right for left, right in zip(human, judge, strict=True)) / count
    )
    human_positive, judge_positive = sum(human) / count, sum(judge) / count
    expected = human_positive * judge_positive + (1 - human_positive) * (
        1 - judge_positive
    )
    if expected == 1:
        raise ValueError("人工与模型都只有同一类，无法计算 kappa")
    return observed, (observed - expected) / (1 - expected)


def _read_human(path: Path, expected: list[str]) -> list[bool]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != COLUMNS:
            raise ValueError("盲标表的三列表头有改动")
        rows = list(reader)
    actual = [_row_hash((row[COLUMNS[0]], row[COLUMNS[1]])) for row in rows]
    if actual != expected:
        raise ValueError("盲标表的条目、原文或行顺序有改动")
    try:
        return [HUMAN_VALUES[row[COLUMNS[2]].strip()] for row in rows]
    except KeyError as exc:
        raise ValueError("每一行须填「有编造」或「没有」") from exc


def calibrate(
    judging_path: Path, sample_map: Path, human_path: Path, output: Path
) -> None:
    judging = _read_json(judging_path)
    _compare_labels(
        _read_json(sample_map), judging, human_path, output, judging_path.name
    )


def validate_challenge(
    cases: Path, judging_path: Path, human_path: Path, output: Path
) -> None:
    from evals.runners.e5_validation import candidates, sample_of

    judging = _read_json(judging_path)
    if judging.get("input_digests") != input_digests([cases]):
        raise ValueError("挑战数据与裁判运行时的文件不一致")
    basis = EvidenceBasis(judging["evidence_basis"])
    sample = sample_of(candidates(cases), basis, cases)
    _compare_labels(sample, judging, human_path, output, judging_path.name)


def _compare_labels(sample, judging, human_path, output, judging_filename):
    if judging["split"] != "dev" or any(
        sample[field] != judging[field] for field in ("dataset", "inputs")
    ):
        raise ValueError("校准必须使用生成盲标表时的开发集")
    if (
        "input_digests" in sample
        and sample["input_digests"] != judging.get("input_digests")
    ) or (
        "evidence_basis" in sample
        and sample["evidence_basis"] != judging.get("evidence_basis")
    ):
        raise ValueError("盲标表与裁判的证据口径或改写文件不一致")
    human = _read_human(human_path, sample["row_hashes"])
    verdicts = _verdicts(judging)
    model = [bool(verdicts[key].types) for key in sample["keys"]]
    try:
        observed, kappa = agreement(human, model)
    except ValueError:
        if human != model or len(set(human)) != 1:
            raise
        observed, kappa = 1.0, None
    labels = list(zip(human, model, strict=True))
    result = dict(
        count=len(human),
        agreement=observed,
        kappa=kappa,
        human_positive=sum(human),
        judge_positive=sum(model),
        missed_rows=[
            i
            for i, (human_label, predicted) in enumerate(labels, 2)
            if human_label and not predicted
        ],
        false_alarm_rows=[
            i
            for i, (human_label, predicted) in enumerate(labels, 2)
            if predicted and not human_label
        ],
        disagreement_rows=[
            index
            for index, labels in enumerate(zip(human, model, strict=True), start=2)
            if labels[0] != labels[1]
        ],
        judge_prompt=judging["judge_prompt"],
        judge_prompt_sha256=judging.get("judge_prompt_sha256"),
        judge_model=judging["model"]["model_name"],
        judging=judging_filename,
        evidence_basis=judging.get("evidence_basis", EvidenceBasis.CONFIRMED),
        source=sample.get("source", "regression"),
        passes=kappa is not None and kappa >= 0.6,
        strata=sample["strata"],
    )
    _save_json(output, result)
    display = f"{kappa:.3f}" if kappa is not None else "无法计算"
    print(f"一致率 {observed:.1%}，Cohen’s kappa {display} ({len(human)} 条)")


@dataclass
class Metrics:
    attempts: list[Attempt]
    verdicts: list[Verdict]

    @property
    def calls(self) -> list[dict[str, Any]]:
        return [
            call
            for attempt in self.attempts
            for call in (*attempt.shared_calls, *attempt.calls)
        ]

    def summary(self) -> dict[str, Any]:
        starts = len(self.attempts)
        bullets = [bullet for attempt in self.attempts for bullet in attempt.bullets]
        status = Counter(bullet.check_status for bullet in bullets)
        final = len(self.verdicts)
        fabricated = sum(bool(verdict.types) for verdict in self.verdicts)
        return dict(
            starts=starts,
            complete=sum(a.status is RunStatus.SUCCEEDED for a in self.attempts),
            output=final,
            average_bullets=final / starts,
            fabricated=fabricated,
            fabrication_rate=fabricated / final if final else None,
            fallback=status[CheckStatus.FALLBACK],
            fallback_rate=status[CheckStatus.FALLBACK] / final if final else None,
            pending=status[CheckStatus.PENDING],
            placeholders=sum(bullet.text.count(PLACEHOLDER) for bullet in bullets),
            fabrication_types=Counter(
                kind for verdict in self.verdicts for kind in verdict.types
            ),
            average_prompt_tokens=sum(c["prompt_tokens"] or 0 for c in self.calls)
            / starts,
            average_completion_tokens=sum(
                c["completion_tokens"] or 0 for c in self.calls
            )
            / starts,
            average_model_seconds=sum(c["latency_ms"] or 0 for c in self.calls)
            / 1000
            / starts,
            average_wall_seconds=sum(
                a.wall_seconds + a.shared_wall_seconds for a in self.attempts
            )
            / starts,
            billed_calls=sum(len(a.calls) for a in self.attempts),
            shared_calls=sum(len(a.shared_calls) for a in self.attempts),
            unknown_usage=sum(
                c["prompt_tokens"] is None or c["completion_tokens"] is None
                for c in self.calls
            ),
        )


def score(inputs: list[Path], judging_path: Path, output: Path) -> None:
    judging = _read_json(judging_path)
    split, records, candidates = _candidates(inputs, judging)
    verdicts = _verdicts(judging)
    if verdicts.keys() != {item.key for item in candidates}:
        raise ValueError("裁判必须覆盖全部定稿条目")
    metrics = {}
    examples = {}
    for record in records:
        version = Version(record["version"])
        rows = [item for item in candidates if item.version is version]
        if version is Version.D and any(
            not item.bullet.fact_ids
            or not set(item.bullet.fact_ids) <= item.pair.facts.keys()
            for item in rows
        ):
            raise ValueError("D 有条目无法追溯到本次已确认的事实")
        metrics[version] = Metrics(
            [Attempt.model_validate(row) for row in record["attempts"]],
            [verdicts[item.key] for item in rows],
        ).summary()
        examples[version] = [
            dict(
                key=item.key,
                sentence=item.bullet.text,
                experience=item.bullet.experience,
                verdict=verdicts[item.key].model_dump(mode="json"),
                evidence=_relevant_quotes(item),
            )
            for item in rows
            if verdicts[item.key].types
        ]
    _save_json(
        output,
        dict(
            split=split,
            dataset=judging["dataset"],
            contrast=metrics,
            evidence_basis=judging.get("evidence_basis", EvidenceBasis.CONFIRMED),
            validation_passed=judging.get("calibration", {}).get("passes"),
            judging=judging_path.name,
            examples=examples,
        ),
    )
    print(f"E5 计分已写入 {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="operation", required=True)
    prepare_args = modes.add_parser("prepare", help="生成中文盲标表")
    prepare_args.add_argument("--sheet", type=Path, default=CALIBRATION_SHEET)
    calibration_args = modes.add_parser("calibrate", help="和人工结果算一致率及 kappa")
    calibration_args.add_argument("--human", type=Path, default=HUMAN_LABELS)
    validate_args = modes.add_parser("validate", help="计算独立挑战样本上的裁判一致性")
    validate_args.add_argument("--cases", required=True, type=Path)
    validate_args.add_argument("--human", required=True, type=Path)
    score_args = modes.add_parser("score", help="计算五个对照组的指标")
    for mode in (prepare_args, score_args):
        mode.add_argument("inputs", type=Path, nargs=5)
    for mode in (prepare_args, calibration_args):
        mode.add_argument("--sample-map", type=Path, default=CALIBRATION_MAP)
    for mode in (prepare_args, calibration_args, validate_args, score_args):
        mode.add_argument("--judging", type=Path, required=True)
    for mode in (calibration_args, validate_args, score_args):
        mode.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.operation == "prepare":
            prepare(args.inputs, args.judging, args.sheet, args.sample_map)
        elif args.operation == "calibrate":
            calibrate(args.judging, args.sample_map, args.human, args.output)
        elif args.operation == "validate":
            validate_challenge(args.cases, args.judging, args.human, args.output)
        else:
            score(args.inputs, args.judging, args.output)
    except (ValueError, FileExistsError, KeyError) as error:
        parser.exit(1, f"{error}\n")
