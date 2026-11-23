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
    role_context,
)
from jobpilot.services.parsing import normalize_text

DATASETS = REPO_ROOT / "evals" / "datasets"
RESUME_ANNOTATIONS = sorted((DATASETS / "annotations").glob("resume-*.json"))


def load(path: Path) -> tuple[str, dict[str, Any]]:
    annotation = json.loads(path.read_text(encoding="utf-8"))
    assert annotation["sample_id"] == path.stem
    resume = DATASETS / "resumes" / f"{path.stem}.txt"
    return normalize_text(resume.read_text(encoding="utf-8")), annotation


def test_dev_set_is_fully_annotated() -> None:
    """与 evals/runners/e1_extract_facts.py 的 DEV_SAMPLES 保持一致。"""
    dev_set = {
        *(f"resume-00{n}" for n in range(1, 6)),
        *(f"resume-0{n}" for n in (11, 15, 21, 22, 23)),
    }
    assert dev_set <= {path.stem for path in RESUME_ANNOTATIONS}


@pytest.mark.parametrize("path", RESUME_ANNOTATIONS, ids=lambda path: path.stem)
def test_annotated_facts_follow_extraction_rules(path: Path) -> None:
    raw_text, annotation = load(path)
    # V1 只支持中文：角色词表没有英文词，英文样本的 lead、own 一定过不了，见 06 T1.6。
    check_role_words = annotation.get("language", "zh") == "zh"
    for fact in annotation["facts"]:
        quote = fact["source_quote"]
        responsibility = Responsibility(fact["responsibility"])
        kind = FactKind(fact["kind"])

        # 唯一定位：评测按位置配对，片段出现多处时配对会有歧义。
        spans = locate_quote(raw_text, quote)
        assert len(spans) == 1, quote
        if check_role_words:
            [(start, end)] = spans
            context = role_context(raw_text, start, end, kind)
            assert responsibility_supported(responsibility, context), quote
        assert not numbers_missing_from_quote(fact["text"], quote), quote
        if contains_number(fact["text"]):
            assert fact["has_number"], fact["text"]
        if fact["needs_confirmation"]:
            assert responsibility is Responsibility.PARTICIPATE, fact["text"]
