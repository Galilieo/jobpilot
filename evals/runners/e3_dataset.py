"""加载 E3/E5 合成样本并将标注 ID 映射到匹配服务所用的稳定 ID。"""

import json
from dataclasses import dataclass
from functools import cache
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict

from evals.runners.e4_ground_check import ANNOTATION_DIR, load_fact_set
from jobpilot.config import REPO_ROOT
from jobpilot.db.models import (
    CriterionField,
    FactKind,
    FactSetCriterion,
    FactSetEducation,
)
from jobpilot.services.criteria_match import CriterionCandidate, EducationCandidate
from jobpilot.services.jobs import JobExtraction, ParsedJob, build_parsed_job
from jobpilot.services.matching import FactCandidate, MatchInputs, ModelMatchVerdict
from jobpilot.services.parsing import normalize_text

DATASETS = REPO_ROOT / "evals" / "datasets"
E3_DIR = DATASETS / "e3"
SPLIT_PATH = DATASETS / "e3-split.json"
Split = Literal["dev", "test"]
Evidence = int | str


class Label(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    pair_id: str
    resume_id: str
    jd_id: str
    requirement_id: str
    verdict: ModelMatchVerdict
    evidence: list[Evidence]
    note: str


@dataclass(frozen=True)
class JobSample:
    snapshot: ParsedJob
    requirement_ids: dict[UUID, str]


@dataclass(frozen=True)
class ResumeSample:
    fact_set_id: UUID
    inputs: MatchInputs
    evidence_ids: dict[str, Evidence]


def pair_id(resume_id: str, jd_id: str) -> str:
    return f"{resume_id}__{jd_id}"


def _stable_id(kind: str, sample_id: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"jobpilot-e3:{kind}:{sample_id}")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@cache
def load_annotation(sample_id: str) -> dict[str, Any]:
    return read_json(ANNOTATION_DIR / f"{sample_id}.json")


def load_split(split: Split) -> tuple[list[str], list[str]]:
    manifest = read_json(SPLIT_PATH)
    return manifest[split], manifest["jd_ids"]


def labels_path(split: Split) -> Path:
    return E3_DIR / f"labels-{split}.jsonl"


def dataset_version(split: Split) -> dict[str, str]:
    """将输入与答案都纳入指纹，防止把不同版本的标注混在一起计分。"""
    resumes, jobs = load_split(split)
    paths = [
        SPLIT_PATH,
        labels_path(split),
        DATASETS / "e4" / "sections.json",
        *(ANNOTATION_DIR / f"{sample}.json" for sample in [*resumes, *jobs]),
        *(DATASETS / "jds" / f"{job}.txt" for job in jobs),
    ]
    digest = sha256()
    for path in paths:
        digest.update(str(path.relative_to(DATASETS)).encode() + b"\0")
        digest.update(sha256(path.read_bytes()).digest())
    return {"version": read_json(SPLIT_PATH)["version"], "sha256": digest.hexdigest()}


def load_labels(split: Split) -> list[Label]:
    """逐项校对配对全量覆盖和引用存在性；无效答案在运行模型前失败。"""
    resumes, jobs = load_split(split)
    expected = {
        (resume_id, jd_id, requirement["requirement_id"])
        for resume_id in resumes
        for jd_id in jobs
        for requirement in load_annotation(jd_id)["requirements"]
    }
    labels = []
    for line in labels_path(split).read_text(encoding="utf-8").splitlines():
        label = Label.model_validate_json(line)
        location = (label.resume_id, label.jd_id, label.requirement_id)
        if location not in expected or label.pair_id != pair_id(*location[:2]):
            raise ValueError(f"未知或重复的标注项 {location}")
        expected.remove(location)
        _validate_evidence(label)
        labels.append(label)
    if expected:
        raise ValueError(f"缺少 {len(expected)} 条 E3 标注")
    return labels


def _validate_evidence(label: Label) -> None:
    resume = load_annotation(label.resume_id)
    allowed = {
        *range(len(resume["facts"])),
        *(f"criteria.{field}" for field, value in resume["criteria"].items() if value),
        *(f"education.{index}" for index, _ in enumerate(resume["education"])),
    }
    refs = set(label.evidence)
    if (
        not refs <= allowed
        or len(refs) != len(label.evidence)
        or bool(refs) != (label.verdict != ModelMatchVerdict.MISSING)
        or not label.note.strip()
    ):
        raise ValueError(
            f"标注的依据或备注无效：{label.pair_id} / {label.requirement_id}"
        )


@cache
def load_job(jd_id: str) -> JobSample:
    annotation = load_annotation(jd_id)
    requirements = [
        {key: row[key] for key in ("text", "type", "source_quote")}
        for row in annotation["requirements"]
    ]
    extraction = JobExtraction(
        basic_fields=annotation["basic_fields"],
        requirements=requirements,
        keywords=annotation["keywords"],
        duties=[],
    )
    raw_text = normalize_text(
        (DATASETS / "jds" / f"{jd_id}.txt").read_text(encoding="utf-8")
    )
    snapshot = build_parsed_job(raw_text, extraction)
    requirement_ids = {}
    for requirement, annotation_row in zip(
        snapshot.requirements, annotation["requirements"], strict=True
    ):
        stable_id = annotation_row["requirement_id"]
        requirement.requirement_id = _stable_id("requirement", stable_id)
        requirement_ids[requirement.requirement_id] = stable_id
    return JobSample(snapshot=snapshot, requirement_ids=requirement_ids)


def _load_criteria(annotation: dict[str, Any]) -> tuple[CriterionCandidate, ...]:
    return tuple(
        CriterionCandidate.from_row(
            FactSetCriterion(
                field=field,
                int_value=entry["value"]
                if field != CriterionField.EARLIEST_START
                else None,
                text_value=entry["value"]
                if field == CriterionField.EARLIEST_START
                else None,
            )
        )
        for field, entry in annotation["criteria"].items()
        if entry is not None
    )


def _load_education(
    resume_id: str, annotation: dict[str, Any]
) -> tuple[EducationCandidate, ...]:
    return tuple(
        EducationCandidate.from_row(
            FactSetEducation(
                education_id=_stable_id("education", f"{resume_id}#{index}"),
                **{
                    field: row[field]
                    for field in (
                        "school",
                        "major",
                        "degree",
                        "status",
                        "start_text",
                        "end_text",
                    )
                },
            )
        )
        for index, row in enumerate(annotation["education"])
    )


def load_resume(resume_id: str, user_id: UUID) -> ResumeSample:
    """沿用 E4 的事实 ID，以及生产匹配的条件、教育显示格式。"""
    annotation = load_annotation(resume_id)
    fact_set_id = _stable_id("fact_set", resume_id)
    facts = tuple(
        FactCandidate(
            fact_id=fact.fact_id,
            lineage_id=fact.fact_id,
            user_id=user_id,
            fact_set_id=fact_set_id,
            experience=fact.experience,
            kind=FactKind(row["kind"]),
            responsibility=fact.responsibility,
            text=fact.text,
            source=fact.source,
        )
        for fact, row in zip(
            load_fact_set(resume_id).values(), annotation["facts"], strict=True
        )
    )
    criteria = _load_criteria(annotation)
    education = _load_education(resume_id, annotation)
    evidence_ids = {
        **{str(fact.fact_id): index for index, fact in enumerate(facts)},
        **{item.ref_id: item.ref_id for item in criteria},
        **{item.ref_id: f"education.{index}" for index, item in enumerate(education)},
    }
    return ResumeSample(
        fact_set_id, MatchInputs(facts, criteria, education), evidence_ids
    )
