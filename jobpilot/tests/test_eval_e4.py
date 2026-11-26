"""E4 评测数据自检与计分逻辑。数据文件还没放进来时跳过数据检查。"""

import json
from collections import Counter
from typing import Any

import pytest
from evals.runners.e4_score import LABELS, NORMAL, render, score_run, semantic_runs

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
    # 补充集只做独立验证，不调规则：句子全新，简历可以和前两组共用，英文的 019 仍不用。
    "dev2": {f"resume-{n:03d}" for n in range(1, 24) if n != 19},
}
# 测试集误拦、2026-11-24 改规则时已经看过的三条事实，补充集不能再用。
SEEN_FACTS = {("resume-016", 4), ("resume-017", 16), ("resume-020", 11)}


def cases(split: str) -> list[dict[str, Any]]:
    path = E4_DIR / f"cases-{split}.jsonl"
    if not path.exists():
        pytest.skip(f"{path.name} 还没有放进来")
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def facts_of(sample_id: str) -> list[dict[str, Any]]:
    data = json.loads((ANNOTATIONS / f"{sample_id}.json").read_text(encoding="utf-8"))
    return data["facts"]


@pytest.mark.parametrize("split", ["dev", "test", "dev2"])
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


def test_supplement_is_balanced_and_avoids_seen_facts() -> None:
    rows = cases("dev2")
    counts = Counter(row["label"] for row in rows)
    assert 55 <= len(rows) <= 65 and counts[NORMAL] >= 40, counts
    assert counts["number"] >= 6 and counts["role"] >= 6, counts
    assert all(counts[label] >= 1 for label in LABELS), counts
    used = {(row["sample_id"], i) for row in rows for i in row["fact_indexes"]}
    assert not used & SEEN_FACTS


def test_sentences_do_not_repeat_across_splits() -> None:
    splits = [{row["sentence"] for row in cases(name)} for name in SPLIT_RESUMES]
    for i, left in enumerate(splits):
        for right in splits[i + 1 :]:
            assert not left & right


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
    report = render(
        result, {"n1": case}, runs, REPO_ROOT / "evals/results/example.json"
    )
    line = next(line for line in report.splitlines() if "`n1`" in line)
    assert "3/3 次" in line
    assert "连接失败（1 次）" in line
    assert "原型设计缺少依据（2 次）" in line


def test_program_only_report_lists_two_layers_and_failures() -> None:
    """runs 为 0（补充集只算程序层）时报告只列前两种组合，误拦和漏拦按 1 次计。"""
    cases = {
        "n1": {
            "case_id": "n1",
            "sentence": "组织 2 位组员联调",
            "label": NORMAL,
            "layer1": [],
            "layer2": [{"kind": "number", "message": "数字 2 没有依据"}],
        },
        "f1": {
            "case_id": "f1",
            "sentence": "负责五个展点",
            "label": "number",
            "layer1": [],
            "layer2": [],
            "note": "三个写成五个",
        },
    }
    result = {
        "split": "dev2",
        "created_at": "test",
        "git": {"commit": "test", "dirty": False},
        "prompt_version": "test",
        "model": {},
        "runs": 0,
        "cases": list(cases.values()),
        "semantic": [],
    }
    runs = [score_run(cases, group) for group in semantic_runs(result, cases)]
    report = render(result, cases, runs, REPO_ROOT / "evals/results/example.json")

    assert runs[0]["summary"]["L12"]["false_block"] == 1
    assert runs[0]["summary"]["L12"]["detection"] == 0
    assert "第一 + 二 + 三层" not in report
    assert "只算第一、二层" in report
    assert "（1/1 次）`n1`" in report and "数字 2 没有依据" in report
    assert "（1/1 次）`f1`" in report
