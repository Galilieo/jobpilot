"""E1 标注自检：标准答案本身要先过程序的规则，否则评测比对的是错的答案。"""

import json
from pathlib import Path
from typing import Any

import pytest

from jobpilot.config import REPO_ROOT
from jobpilot.db.models import FactKind, Responsibility
from jobpilot.services.facts import (
    contains_number,
    locate_quote,
    numbers_missing_from_quote,
    responsibility_supported,
)
from jobpilot.services.parsing import normalize_text

DATASETS = REPO_ROOT / "evals" / "datasets"
RESUME_ANNOTATIONS = sorted((DATASETS / "annotations").glob("resume-*.json"))


def load(path: Path) -> tuple[str, list[dict[str, Any]]]:
    annotation = json.loads(path.read_text(encoding="utf-8"))
    assert annotation["sample_id"] == path.stem
    resume = DATASETS / "resumes" / f"{path.stem}.txt"
    return normalize_text(resume.read_text(encoding="utf-8")), annotation["facts"]


def test_dev_set_is_fully_annotated() -> None:
    assert [path.stem for path in RESUME_ANNOTATIONS][:5] == [
        f"resume-00{n}" for n in range(1, 6)
    ]


@pytest.mark.parametrize("path", RESUME_ANNOTATIONS, ids=lambda path: path.stem)
def test_annotated_facts_follow_extraction_rules(path: Path) -> None:
    raw_text, facts = load(path)
    for fact in facts:
        quote = fact["source_quote"]
        responsibility = Responsibility(fact["responsibility"])
        FactKind(fact["kind"])

        # 唯一定位：评测按位置配对，片段出现多处时配对会有歧义。
        assert len(locate_quote(raw_text, quote)) == 1, quote
        assert responsibility_supported(responsibility, quote), quote
        assert not numbers_missing_from_quote(fact["text"], quote), quote
        if contains_number(fact["text"]):
            assert fact["has_number"], fact["text"]
        if fact["needs_confirmation"]:
            assert responsibility is Responsibility.PARTICIPATE, fact["text"]
