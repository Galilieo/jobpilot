"""用未参与试跑的合成开发集配对制作 E5 裁判挑战盲标表。"""

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from random import Random
from typing import Any

from evals.runners.e3_dataset import DATASETS
from evals.runners.e5_dataset import dataset_version, pairs_for
from evals.runners.e5_judge import (
    Candidate,
    EvidenceBasis,
    _batches,
    input_digests,
    read_outputs,
)
from evals.runners.e5_run import EvalBullet, Version
from evals.runners.e5_score import (
    COLUMNS,
    PER_VERSION,
    _row_hash,
    _save_json,
    _take_distinct,
)

DIRECTORY = DATASETS / "e5"
CASES = DIRECTORY / "independent-cases.jsonl"
PILOT_PAIRS = {"resume-001__jd-syn-01", "resume-011__jd-syn-03"}


def candidates(path: Path = CASES) -> list[Candidate]:
    pairs = {pair.key: pair for pair in pairs_for("dev") if pair.key not in PILOT_PAIRS}
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    result = [
        Candidate(
            Version(row["version"]),
            pairs[row["pair_id"]],
            row["id"],
            EvalBullet(experience=row["experience"], text=row["text"]),
        )
        for row in rows
    ]
    if Counter(item.version for item in result) != dict.fromkeys(Version, PER_VERSION):
        raise ValueError("独立验证须包含每个版本各八条")
    if [item.index for item in result] != list(range(1, len(result) + 1)):
        raise ValueError("独立挑战样本 ID 与行号不一致")
    return result


def _evidence(item: Candidate, basis: EvidenceBasis) -> str:
    if basis is EvidenceBasis.RESUME:
        return item.pair.resume_text.strip()
    annotation = item.pair.annotation
    entries = [
        f"【{fact['experience']}】{fact['text']}（{fact['responsibility']}）\n原句：{fact['source_quote']}"
        for fact in annotation["facts"]
    ]
    entries.extend(
        f"【求职条件】{entry['source_quote']}"
        for entry in annotation["criteria"].values()
        if entry
    )
    entries.extend(
        f"【教育】{entry['source_quote']}" for entry in annotation["education"]
    )
    return "\n".join(entries)


def annotation_rows(
    rows: list[Candidate], basis: EvidenceBasis
) -> list[tuple[str, str]]:
    return [
        (
            f"【{item.bullet.experience or '技能概述'}】{item.bullet.text}",
            _evidence(item, basis),
        )
        for item in rows
    ]


def sample_of(
    rows: list[Candidate], basis: EvidenceBasis, path: Path
) -> dict[str, Any]:
    return dict(
        keys=[item.key for item in rows],
        row_hashes=[_row_hash(row) for row in annotation_rows(rows, basis)],
        strata=Counter(item.version for item in rows),
        evidence_basis=basis,
        dataset=dataset_version("dev"),
        inputs=[path.name],
        source="authored_challenge",
    )


def _write_sheet(path: Path, rows: list[Candidate], basis: EvidenceBasis) -> None:
    with path.open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(COLUMNS)
        writer.writerows((*row, "") for row in annotation_rows(rows, basis))


def draw_from_development(pool: list[Candidate]) -> list[Candidate]:
    random = Random(20261128)
    seen: set[tuple[str, str | None, str]] = set()
    rows = []
    for version in Version:
        candidates = [
            item
            for item in pool
            if item.version is version and item.pair.key not in PILOT_PAIRS
        ]
        if len(candidates) < PER_VERSION:
            raise ValueError(f"{version} 的独立开发集条目不足 {PER_VERSION}")
        random.shuffle(candidates)
        rows.extend(_take_distinct(candidates, PER_VERSION, seen))
    random.shuffle(rows)
    return rows


def prepare_from_outputs(paths: list[Path], sheet_dir: Path, map_dir: Path) -> None:
    split, records = read_outputs(paths)
    if split != "dev":
        raise ValueError("独立盲标只能从开发集输出抽样")
    rows = draw_from_development(
        [item for batch in _batches(split, records, None) for item in batch.candidates]
    )
    for basis in EvidenceBasis:
        sheet = sheet_dir / f"independent-output-{basis}.csv"
        _write_sheet(sheet, rows, basis)
        sample = sample_of(rows, basis, sheet)
        sample.update(
            source="generated_dev",
            inputs=[path.name for path in paths],
            input_digests=input_digests(paths),
        )
        _save_json(map_dir / f"independent-output-{basis}-map.json", sample)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", type=Path, nargs="*")
    parser.add_argument("--sheet-dir", type=Path, default=DIRECTORY)
    parser.add_argument("--map-dir", type=Path, required=False)
    args = parser.parse_args()
    if args.inputs:
        if args.map_dir is None:
            parser.error("抽取模型输出时须指定 --map-dir")
        prepare_from_outputs(args.inputs, args.sheet_dir, args.map_dir)
    else:
        rows = candidates()
        for basis in EvidenceBasis:
            _write_sheet(args.sheet_dir / f"independent-{basis}.csv", rows, basis)
