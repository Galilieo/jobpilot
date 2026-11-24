"""E4 评测数据自检与计分逻辑。数据文件还没放进来时跳过数据检查。"""

import json
from collections import Counter
from typing import Any

import pytest
from evals.runners.e4_score import LABELS, NORMAL, render, score_run

from jobpilot.config import REPO_ROOT

E4_DIR = REPO_ROOT / "evals" / "datasets" / "e4"
ANNOTATIONS = REPO_ROOT / "evals" / "datasets" / "annotations"
# 与 E1 分组一致：开发集简历只用于开发集句子，测试集同理。resume-019 是英文，不用。
SPLIT_RESUMES = {
    "dev": {"resume-011", "resume-015", "resume-021", "resume-022", "resume-023"},
    "test": {
        "resume-012",
        "resume-013",
        "resume-014",
        "resume-016",
        "resume-017",
        "resume-018",
        "resume-020",
    },
}


def cases(split: str) -> list[dict[str, Any]]:
    path = E4_DIR / f"cases-{split}.jsonl"
    if not path.exists():
        pytest.skip(f"{path.name} 还没有放进来")
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def facts_of(sample_id: str) -> list[dict[str, Any]]:
    data = json.loads((ANNOTATIONS / f"{sample_id}.json").read_text(encoding="utf-8"))
    return data["facts"]


@pytest.mark.parametrize("split", ["dev", "test"])
def test_cases_are_well_formed(split: str) -> None:
    rows = cases(split)
    ids = [row["case_id"] for row in rows]
    assert len(ids) == len(set(ids)), "case_id 重复"
    for row in rows:
        where = row["case_id"]
        assert row["case_id"].startswith(f"e4-{split}-"), where
        assert row["sample_id"] in SPLIT_RESUMES[split], where
        assert row["label"] == NORMAL or row["label"] in LABELS, where
        assert row["section"] in ("experience", "skills"), where
        assert 0 < len(row["sentence"].strip()) <= 200, where
        facts = facts_of(row["sample_id"])
        assert row["fact_indexes"], where
        assert all(0 <= i < len(facts) for i in row["fact_indexes"]), where
        experiences = {fact["experience"] for fact in facts}
        if row.get("experience") is not None:
            assert row["experience"] in experiences, where
        if row["label"] != NORMAL:
            assert row.get("note"), f"{where}：编造句要写明植入了什么"


@pytest.mark.parametrize("split", ["dev", "test"])
def test_cases_are_balanced(split: str) -> None:
    counts = Counter(row["label"] for row in cases(split))
    assert 140 <= sum(counts.values()) <= 160, counts
    assert counts[NORMAL] >= 70, counts
    for label in LABELS:
        assert counts[label] >= 10, f"{label} 只有 {counts[label]} 句"


def test_sentences_do_not_repeat_across_splits() -> None:
    dev = {row["sentence"] for row in cases("dev")}
    test = {row["sentence"] for row in cases("test")}
    assert not dev & test


def test_score_run_counts_each_configuration() -> None:
    case_rows = {
        "n1": {"label": NORMAL, "layer1": [], "layer2": []},
        "n2": {"label": NORMAL, "layer1": [], "layer2": [{"kind": "number"}]},
        "f1": {"label": "number", "layer1": [], "layer2": [{"kind": "number"}]},
        "f2": {"label": "implicit", "layer1": [], "layer2": []},
        "f3": {"label": "implicit", "layer1": [], "layer2": []},
    }
    semantic = [
        {"case_id": "n1", "error": None, "issues": []},
        {"case_id": "n2", "error": None, "issues": []},
        {"case_id": "f1", "error": None, "issues": []},
        {"case_id": "f2", "error": None, "issues": [{"kind": "semantic"}]},
        {"case_id": "f3", "error": "boom", "issues": []},  # 出错按拦下计
    ]
    result = score_run(case_rows, semantic)
    summary = result["summary"]
    assert summary["L12"]["detection"] == pytest.approx(1 / 3)
    assert summary["L12"]["false_block"] == pytest.approx(1 / 2)
    assert summary["L123"]["detection"] == 1
    assert summary["L123"]["by_label"]["implicit"] == 1
    assert result["first_layer"] == Counter({"第二层": 1, "第三层": 2})


def test_report_counts_each_false_block_reason_across_runs() -> None:
    case = {
        "case_id": "n1",
        "sentence": "常用 Figma 设计原型",
        "label": NORMAL,
        "layer1": [],
        "layer2": [],
    }
    records = [
        {
            "case_id": "n1",
            "error": None,
            "calls": [],
            "issues": [{"kind": kind, "message": reason}],
        }
        for kind, reason in [
            ("check_unavailable", "连接失败"),
            ("semantic", "原型设计缺少依据"),
            ("semantic", "原型设计缺少依据"),
        ]
    ]
    result = {
        "split": "test",
        "created_at": "test",
        "git": {"commit": "test", "dirty": False},
        "prompt_version": "test",
        "model": {},
        "runs": 3,
        "cases": [case],
        "semantic": records,
    }
    runs = [score_run({"n1": case}, [record]) for record in records]
    report = render(result, runs, REPO_ROOT / "evals/results/example.json")
    line = next(line for line in report.splitlines() if "`n1`" in line)
    assert "3/3 次" in line
    assert "连接失败（1 次）" in line
    assert "原型设计缺少依据（2 次）" in line
