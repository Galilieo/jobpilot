"""生成 E3 第二人盲标表，或计算与原标注的一致率和 Cohen's kappa。"""

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from random import Random
from typing import Any

from pydantic import BaseModel

from evals.runners.e3_dataset import (
    E3_DIR,
    Label,
    Split,
    load_annotation,
    load_job,
    load_labels,
)
from jobpilot.config import REPO_ROOT
from jobpilot.services.criteria_match import parse_requirement
from jobpilot.services.matching import ModelMatchVerdict

SAMPLE_COUNT = 50
RANDOM_SEED = 20261126
SHEET = E3_DIR / "second-annotator-sheet.csv"
KEY = E3_DIR / "second-annotator-key.jsonl"
FILLED_SHEET = REPO_ROOT / "evals" / "private" / "e3-second-annotator.csv"
VERDICT_OPTIONS = {
    "已覆盖": ModelMatchVerdict.COVERED,
    "部分覆盖": ModelMatchVerdict.PARTIAL,
    "未覆盖": ModelMatchVerdict.MISSING,
    **{verdict.value: verdict for verdict in ModelMatchVerdict},
}


class SampleKey(BaseModel):
    sample_id: str
    split: Split
    pair_id: str
    requirement_id: str


def _stratum(label: Label) -> tuple[str, str, str]:
    job = load_job(label.jd_id)
    requirement = next(
        item
        for item in job.snapshot.requirements
        if job.requirement_ids[item.requirement_id] == label.requirement_id
    )
    parsed = parse_requirement(requirement, basic_fields=job.snapshot.basic_fields)
    source = "program" if parsed.clauses and not parsed.needs_review else "model"
    return label.verdict.value, requirement.type.value, source


def _sample_split(labels: list[Label], random: Random, count: int) -> list[Label]:
    """在每个实有的交叉类别中轮流取一条，避免稀有判断被占比遮住。"""
    strata = defaultdict(list)
    for label in labels:
        strata[_stratum(label)].append(label)
    buckets = [strata[key] for key in sorted(strata)]
    random.shuffle(buckets)
    for bucket in buckets:
        random.shuffle(bucket)
    selected = []
    while buckets and len(selected) < count:
        for bucket in buckets:
            selected.append(bucket.pop())
            if len(selected) == count:
                break
        buckets = [bucket for bucket in buckets if bucket]
    if len(selected) < count:
        raise ValueError("可抽样的标注不足。")
    return selected


def _sheet_row(sample_id: str, label: Label) -> dict[str, str]:
    resume = load_annotation(label.resume_id)
    requirement = next(
        row
        for row in load_annotation(label.jd_id)["requirements"]
        if row["requirement_id"] == label.requirement_id
    )
    facts = "\n".join(
        f"{index}｜{row['experience']}｜{row['text']}｜原文：{row['source_quote']}"
        for index, row in enumerate(resume["facts"])
    )
    criteria = "\n".join(
        f"criteria.{name}｜{row['source_quote']}"
        for name, row in resume["criteria"].items()
        if row is not None
    )
    education = "\n".join(
        f"education.{index}｜{row['source_quote']}"
        for index, row in enumerate(resume["education"])
    )
    return {
        "sample_id": sample_id,
        "要求原文": requirement["source_quote"],
        "类型": "硬性" if requirement["type"] == "hard" else "加分",
        "E1 事实与原文": facts,
        "求职条件": criteria,
        "教育": education,
        "判断": "",
    }


def prepare() -> None:
    random = Random(RANDOM_SEED)
    selected = [
        (split, label)
        for split in ("dev", "test")
        for label in _sample_split(load_labels(split), random, SAMPLE_COUNT // 2)
    ]
    random.shuffle(selected)
    keys = []
    rows = []
    for index, (split, label) in enumerate(selected, 1):
        sample_id = f"E3-{index:02d}"
        keys.append(
            SampleKey(
                sample_id=sample_id,
                split=split,
                pair_id=label.pair_id,
                requirement_id=label.requirement_id,
            )
        )
        rows.append(_sheet_row(sample_id, label))

    if SHEET.exists() or KEY.exists():
        raise FileExistsError("第二人盲标文件已存在。")
    with SHEET.open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    with KEY.open("x", encoding="utf-8") as stream:
        stream.writelines(key.model_dump_json() + "\n" for key in keys)


def _read_filled(path: Path, allowed: set[str]) -> dict[str, ModelMatchVerdict]:
    answers = {}
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not {"sample_id", "判断"} <= set(reader.fieldnames or []):
            raise ValueError("盲标表必须保留 sample_id 和 判断 两列。")
        for row in reader:
            sample_id = (row["sample_id"] or "").strip()
            verdict = (row["判断"] or "").strip()
            if (
                sample_id not in allowed
                or sample_id in answers
                or verdict not in VERDICT_OPTIONS
            ):
                raise ValueError(f"盲标行有未知或重复 ID，或判断无效：{sample_id}")
            answers[sample_id] = VERDICT_OPTIONS[verdict]
    if answers.keys() != allowed:
        raise ValueError(f"缺少标注：{', '.join(sorted(allowed - answers.keys()))}")
    return answers


def agreement(
    pairs: list[tuple[ModelMatchVerdict, ModelMatchVerdict]],
) -> tuple[float, float | None]:
    """完全一致率与扣除按各自类别分布随机一致的 Cohen's kappa。"""
    count = len(pairs)
    first = Counter(a for a, _ in pairs)
    second = Counter(b for _, b in pairs)
    agreed = sum(a == b for a, b in pairs)
    expected_count_squared = sum(first[v] * second[v] for v in ModelMatchVerdict)
    kappa_denominator = count * count - expected_count_squared
    kappa = (
        (agreed * count - expected_count_squared) / kappa_denominator
        if kappa_denominator
        else None
    )
    return agreed / count, kappa


def compare(path: Path) -> dict[str, Any]:
    keys = [
        SampleKey.model_validate_json(line)
        for line in KEY.read_text(encoding="utf-8").splitlines()
    ]
    allowed = {sample.sample_id for sample in keys}
    if len(keys) != SAMPLE_COUNT or len(allowed) != SAMPLE_COUNT:
        raise ValueError("盲标键表必须含 50 个唯一抽样 ID。")
    second = _read_filled(path, allowed)
    first = {
        (split, label.pair_id, label.requirement_id): label
        for split in ("dev", "test")
        for label in load_labels(split)
    }
    pairs = []
    disagreements = []
    for key in keys:
        original = first[key.split, key.pair_id, key.requirement_id]
        independent = second[key.sample_id]
        pairs.append((original.verdict, independent))
        if original.verdict != independent:
            disagreements.append(
                {
                    **key.model_dump(),
                    "first_verdict": original.verdict.value,
                    "second_verdict": independent.value,
                }
            )
    rate, kappa = agreement(pairs)
    return {
        "samples": len(pairs),
        "agreement": rate,
        "cohens_kappa": kappa,
        "disagreements": disagreements,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prepare", action="store_true", help="开发与测试标注齐备后生成空白表"
    )
    parser.add_argument("--input", type=Path, default=FILLED_SHEET, help="填好的盲标表")
    args = parser.parse_args()
    if args.prepare:
        prepare()
    else:
        print(json.dumps(compare(args.input), ensure_ascii=False, indent=2))
