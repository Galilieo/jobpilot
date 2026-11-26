"""按轮统计 E3 的三分类、引用错误和稳定性，生成 Markdown 汇总。"""

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean
from typing import Any, Literal

from pydantic import BaseModel

from evals.runners.e3_dataset import (
    Evidence,
    Label,
    Split,
    dataset_version,
    load_annotation,
    load_labels,
    read_json,
)
from jobpilot.services.jobs import RequirementType
from jobpilot.services.matching import ModelMatchVerdict

VERDICTS = tuple(ModelMatchVerdict)
GROUPS = ("总计", "硬性", "加分", "程序", "模型")
VERDICT_TEXT = dict(zip(VERDICTS, ("已覆盖", "部分覆盖", "未覆盖"), strict=True))


class Prediction(BaseModel):
    requirement_id: str
    verdict: ModelMatchVerdict
    decision_source: Literal["program", "model"]
    evidence: list[Evidence]
    reason: str


class Attempt(BaseModel):
    run_index: int
    pair_id: str
    error: str | None
    items: list[Prediction]


@dataclass
class Metrics:
    matrix: Counter[tuple[ModelMatchVerdict, ModelMatchVerdict]] = field(
        default_factory=Counter
    )
    covered: int = 0
    evidence_hits: int = 0
    evidence_extra: int = 0

    def add(self, label: Label, prediction: Prediction) -> None:
        self.matrix[label.verdict, prediction.verdict] += 1
        if prediction.verdict == ModelMatchVerdict.COVERED:
            actual, expected = set(prediction.evidence), set(label.evidence)
            self.covered += 1
            self.evidence_hits += bool(actual & expected)
            self.evidence_extra += bool(actual - expected)

    @property
    def count(self) -> int:
        return self.matrix.total()

    @property
    def correct(self) -> int:
        return sum(self.matrix[verdict, verdict] for verdict in VERDICTS)


@dataclass
class Score:
    by_run: dict[int, dict[str, Metrics]]
    overall: dict[str, Metrics] = field(default_factory=lambda: _new_groups())
    verdicts: dict[tuple[str, str], list[ModelMatchVerdict]] = field(
        default_factory=dict
    )
    failed: Counter[str] = field(default_factory=Counter)
    mismatches: list[tuple[int, Label, Prediction]] = field(default_factory=list)
    total_pairs: int = 0


def _new_groups() -> dict[str, Metrics]:
    return {name: Metrics() for name in GROUPS}


def _pairs(labels: list[Label]) -> dict[str, dict[str, Label]]:
    pairs: dict[str, dict[str, Label]] = defaultdict(dict)
    for label in labels:
        pairs[label.pair_id][label.requirement_id] = label
    return pairs


def _hardness(label: Label) -> str:
    requirements = load_annotation(label.jd_id)["requirements"]
    row = next(
        row for row in requirements if row["requirement_id"] == label.requirement_id
    )
    return "硬性" if row["type"] == RequirementType.HARD else "加分"


def _score_attempt(score: Score, attempt: Attempt, labels: dict[str, Label]) -> None:
    if attempt.error:
        if attempt.items:
            raise ValueError(f"失败的配对出现了结果条目：{attempt.pair_id}")
        score.failed[attempt.error] += 1
        return
    predictions = {item.requirement_id: item for item in attempt.items}
    if predictions.keys() != labels.keys() or len(predictions) != len(attempt.items):
        raise ValueError(f"配对结果的要求缺失、未知或重复：{attempt.pair_id}")
    for requirement_id, label in labels.items():
        prediction = predictions[requirement_id]
        source = "程序" if prediction.decision_source == "program" else "模型"
        for group in ("总计", _hardness(label), source):
            score.by_run[attempt.run_index][group].add(label, prediction)
            score.overall[group].add(label, prediction)
        key = (attempt.pair_id, requirement_id)
        score.verdicts.setdefault(key, []).append(prediction.verdict)
        if prediction.verdict != label.verdict:
            score.mismatches.append((attempt.run_index, label, prediction))


def score_attempts(labels: list[Label], attempts: list[Attempt], runs: int) -> Score:
    """失败及中断的配对不进入混淆矩阵，按每轮分开记录实际覆盖。"""
    if runs < 1:
        raise ValueError("评测轮数必须大于 0。")
    pairs = _pairs(labels)
    score = Score(
        by_run={index: _new_groups() for index in range(1, runs + 1)},
        total_pairs=len(pairs),
    )
    seen = set()
    for attempt in attempts:
        key = (attempt.run_index, attempt.pair_id)
        if key in seen or key[0] not in score.by_run or key[1] not in pairs:
            raise ValueError(f"未知或重复的运行记录：{key}")
        seen.add(key)
        _score_attempt(score, attempt, pairs[attempt.pair_id])
    return score


def _percent(numerator: int, denominator: int) -> str:
    return (
        f"{numerator / denominator:.1%}（{numerator}/{denominator}）"
        if denominator
        else "—"
    )


def _summary_row(name: str, metrics: Metrics) -> str:
    agreement = _percent(metrics.correct, metrics.count)
    hits = _percent(metrics.evidence_hits, metrics.covered)
    extra = _percent(metrics.evidence_extra, metrics.covered)
    return f"| {name} | {agreement} | {hits} | {extra} |"


def _rates_by_run(score: Score, group: str, successes: str, denominator: str) -> str:
    metrics = [groups[group] for groups in score.by_run.values()]
    values = [
        getattr(metric, successes) / getattr(metric, denominator)
        for metric in metrics
        if getattr(metric, denominator)
    ]
    return (
        f"{mean(values):.1%}（{min(values):.1%}～{max(values):.1%}）" if values else "—"
    )


def _summary_lines(raw: dict[str, Any], score: Score) -> list[str]:
    total_attempts = score.total_pairs * len(score.by_run)
    successful = len(raw["attempts"]) - score.failed.total()
    completion = _percent(successful, total_attempts)
    unfinished = total_attempts - len(raw["attempts"])
    return [
        f"# E3 匹配判断 · {raw['split']}",
        "",
        (
            f"- 可计分配对运行 {completion}，失败 {score.failed.total()}，"
            f"未完成 {unfinished}。"
        ),
        (
            f"- 输入指纹 `{raw['dataset']['sha256']}`，代码 `{raw['git']['commit']}`，"
            f"提示词 `{raw['prompt_version']}`，模型 `{raw['model']['model_name']}`。"
        ),
        f"- 失败类型 {dict(score.failed) or '无'}。",
        "- 标注一致性待测。",
        "",
        "## 各轮汇总",
        "",
        "| 分组 | 三分类一致率 | 覆盖时依据命中率 | 覆盖时依据多引率 |",
        "| --- | ---: | ---: | ---: |",
        *(_summary_row(group, score.overall[group]) for group in GROUPS),
        "",
        "## 每轮平均与最小～最大",
        "",
        "| 分组 | 三分类一致率 | 依据命中率 | 依据多引率 |",
        "| --- | ---: | ---: | ---: |",
        *(
            f"| {group} | {_rates_by_run(score, group, 'correct', 'count')} | "
            f"{_rates_by_run(score, group, 'evidence_hits', 'covered')} | "
            f"{_rates_by_run(score, group, 'evidence_extra', 'covered')} |"
            for group in GROUPS
        ),
        "",
        "## 逐轮总计",
        "",
        "| 轮次 | 三分类一致率 | 依据命中率 | 依据多引率 |",
        "| --- | ---: | ---: | ---: |",
        *(
            _summary_row(str(index), group["总计"])
            for index, group in score.by_run.items()
        ),
    ]


def _matrix_lines(score: Score) -> list[str]:
    lines = ["", "## 混淆矩阵", "", "行是标注，列是服务判定。"]
    for group, metrics in score.overall.items():
        lines.extend(
            [
                "",
                f"### {group}",
                "",
                "| 标注 \\ 判定 | 已覆盖 | 部分覆盖 | 未覆盖 |",
                "| --- | ---: | ---: | ---: |",
            ]
        )
        for expected in VERDICTS:
            counts = " | ".join(
                str(metrics.matrix[expected, actual]) for actual in VERDICTS
            )
            lines.append(f"| {VERDICT_TEXT[expected]} | {counts} |")
    return lines


def _stability_lines(score: Score) -> list[str]:
    runs = len(score.by_run)
    comparable = [
        verdicts for verdicts in score.verdicts.values() if len(verdicts) == runs
    ]
    unstable = sum(len(set(verdicts)) > 1 for verdicts in comparable)
    statement = f"各轮均有结果的 {len(comparable)} 条中，有 {unstable} 条结论不一致。"
    return ["", "## 跨轮稳定性", "", statement if runs > 1 else "只运行了一轮。"]


def report_lines(raw: dict[str, Any], score: Score) -> list[str]:
    return [
        *_summary_lines(raw, score),
        *_matrix_lines(score),
        *_stability_lines(score),
        "",
        "## 分类不一致明细",
        "",
        *(
            f"- 第 {index} 轮 `{label.pair_id}` · `{label.requirement_id}` "
            f"标注 {VERDICT_TEXT[label.verdict]}，"
            f"服务 {VERDICT_TEXT[prediction.verdict]}"
            f"（{prediction.decision_source}）。 "
            f"标注依据 {label.evidence}，服务依据 {prediction.evidence}。 "
            f"标注说明 {label.note} 服务说明 {prediction.reason}"
            for index, label, prediction in score.mismatches
        ),
    ]


def main(path: Path, report: Path | None) -> None:
    raw = read_json(path)
    split: Split = raw["split"]
    if raw["eval"] != "E3" or split not in ("dev", "test"):
        raise ValueError("结果文件不是有效的 E3 运行。")
    for key, value in dataset_version(split).items():
        if raw["dataset"].get(key) != value:
            raise ValueError("结果文件与当前 E3 数据指纹不同。")
    attempts = [Attempt.model_validate(row) for row in raw["attempts"]]
    score = score_attempts(load_labels(split), attempts, raw["runs"])
    markdown = "\n".join(report_lines(raw, score)) + "\n"
    if report is None:
        print(markdown, end="")
    else:
        with report.open("x", encoding="utf-8") as stream:
            stream.write(markdown)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result", type=Path)
    parser.add_argument("--report", type=Path, help="Markdown 报告输出路径")
    args = parser.parse_args()
    main(args.result, args.report)
