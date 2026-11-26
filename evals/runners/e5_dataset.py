"""用 E3 配对和 E1 标注装载 E5 的公开合成输入。"""

from dataclasses import dataclass
from hashlib import sha256
from itertools import product
from pathlib import Path
from typing import Any
from uuid import UUID

from evals.runners.e3_dataset import (
    DATASETS,
    SPLIT_PATH,
    JobSample,
    Split,
    load_annotation,
    load_job,
    load_split,
    pair_id,
    read_json,
)
from evals.runners.e4_ground_check import load_fact_set
from jobpilot.services.grounding import GroundingFact


@dataclass(frozen=True)
class Pair:
    resume_id: str
    jd_id: str

    @property
    def key(self) -> str:
        return pair_id(self.resume_id, self.jd_id)

    @property
    def resume_text(self) -> str:
        return _raw("resumes", self.resume_id)

    @property
    def jd_text(self) -> str:
        return _raw("jds", self.jd_id)

    @property
    def job(self) -> JobSample:
        return load_job(self.jd_id)

    @property
    def annotation(self) -> dict[str, Any]:
        return load_annotation(self.resume_id)

    @property
    def facts(self) -> dict[UUID, GroundingFact]:
        return load_fact_set(self.resume_id)


def _raw(folder: str, sample: str) -> str:
    return (DATASETS / folder / f"{sample}.txt").read_text(encoding="utf-8")


def pairs_for(split: Split, selected: list[str] | None = None) -> list[Pair]:
    resumes, jobs = load_split(split)
    pairs = {pair.key: pair for pair in (Pair(r, j) for r, j in product(resumes, jobs))}
    if selected is None:
        return list(pairs.values())
    if len(set(selected)) != len(selected) or set(selected) - pairs.keys():
        raise ValueError("配对 ID 必须属于指定分组且不得重复")
    return [pairs[key] for key in selected]


def dataset_paths(split: Split) -> list[Path]:
    resumes, jobs = load_split(split)
    return [
        SPLIT_PATH,
        DATASETS / "e4" / "sections.json",
        *(DATASETS / "resumes" / f"{sample}.txt" for sample in resumes),
        *(DATASETS / "jds" / f"{sample}.txt" for sample in jobs),
        *(DATASETS / "annotations" / f"{sample}.json" for sample in [*resumes, *jobs]),
    ]


def dataset_version(split: Split) -> dict[str, str]:
    digest = sha256()
    for path in dataset_paths(split):
        digest.update(str(path.relative_to(DATASETS)).encode() + b"\0")
        digest.update(sha256(path.read_bytes()).digest())
    return {
        "version": "e5-v1/" + read_json(SPLIT_PATH)["version"],
        "sha256": digest.hexdigest(),
    }


def confirmed_evidence(pair: Pair) -> dict[str, Any]:
    """独立裁判可见全库内容，仍保留事实原文、职责和所属经历。"""
    annotation = pair.annotation
    return {
        "facts": [
            {
                "experience": row["experience"],
                "text": row["text"],
                "source_quote": row["source_quote"],
                "responsibility": row["responsibility"],
            }
            for row in annotation["facts"]
        ],
        "criteria": {
            field: row for field, row in annotation["criteria"].items() if row
        },
        "education": annotation["education"],
    }
