"""E2 第二步：把 JD 解析结果与标注对照计分。不调用模型，可以反复运行。

    uv run --project jobpilot python -m evals.runners.e2_score
    uv run --project jobpilot python -m evals.runners.e2_score evals/results/e2-xxx.json

口径见 04 第 3 节 E2 与第 1.2 节的预期。要求条目的配对与 E1 相同：原文位置重叠才是候选，
候选中按文字相似度做总分最大的一对一配对。标注和报告都引用 JD 原文，所以都放在
Git 忽略的 evals/private/；仓库只记录汇总指标。
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from statistics import mean
from typing import Any

from evals.runners.e1_score import Item, match, pct
from jobpilot.config import REPO_ROOT
from jobpilot.services.facts import locate_quote
from jobpilot.services.keywords import normalize_keywords

RESULT_DIR = REPO_ROOT / "evals" / "results"
ANNOTATION_DIR = REPO_ROOT / "evals" / "private" / "annotations"
REPORT_DIR = REPO_ROOT / "evals" / "private" / "reports"

FIELDS = (
    "company",
    "title",
    "location",
    "salary",
    "onsite_days_per_week",
    "internship_duration",
    "education",
    "graduation_window",
    "application_deadline",
)
# 04 第 1.2 节的初始预期。
EXPECTATIONS = {"requirement_recall": 0.85, "keyword_f1": 0.80}
LABELS = {
    "requirement_recall": "要求条目召回率",
    "hard_recall": "其中：硬性要求召回率",
    "extra_rate": "没配上的预测占比",
    "type_accuracy": "硬性 / 加分分类准确率",
    "quote_verified_rate": "原文片段校验通过率",
    "keyword_precision": "关键词精确率",
    "keyword_recall": "关键词召回率",
    "keyword_f1": "关键词 F1",
    "field_accuracy": "基本字段准确率",
    "field_invented": "基本字段编造率（原文没写却填了）",
}


def _same(a: str | None, b: str | None) -> bool:
    def key(value: str) -> str:
        return "".join(value.split()).casefold()

    return (a is None) == (b is None) and (a is None or key(a) == key(b or ""))


def _gold_items(raw_text: str, requirements: list[dict[str, Any]]) -> list[Item]:
    items = []
    for requirement in requirements:
        spans = locate_quote(raw_text, requirement["source_quote"])
        start, end = spans[0] if spans else (None, None)
        items.append(Item(requirement, start, end))
    return items


def score_record(record: dict[str, Any], raw_text: str, gold: dict[str, Any]) -> dict:
    parsed = record["parsed"]
    golds = _gold_items(raw_text, gold["requirements"])
    preds = [Item(r, r["span_start"], r["span_end"]) for r in parsed["requirements"]]
    pairs = match(preds, golds)
    matched_g = {g for _, g, _ in pairs}
    matched_p = {p for p, _, _ in pairs}

    gold_keywords = {k.casefold() for k in normalize_keywords(gold["keywords"])}
    pred_keywords = {k.casefold() for k in normalize_keywords(parsed["keywords"])}

    fields = []
    for name in FIELDS:
        expected = gold["basic_fields"].get(name)
        actual = parsed["basic_fields"].get(name)
        fields.append((name, expected, actual, _same(expected, actual)))

    return {
        "gold": len(golds),
        "gold_hard": sum(g.data["type"] == "hard" for g in golds),
        "pred": len(preds),
        "verified": sum(bool(p.data["quote_verified"]) for p in preds),
        "matched": len(pairs),
        "matched_hard": sum(golds[g].data["type"] == "hard" for _, g, _ in pairs),
        "type_ok": sum(
            preds[p].data["type"] == golds[g].data["type"] for p, g, _ in pairs
        ),
        "missed": [golds[g].text for g in range(len(golds)) if g not in matched_g],
        "extra": [preds[p].text for p in range(len(preds)) if p not in matched_p],
        "type_errors": [
            f"{golds[g].text}：{preds[p].data['type']} → {golds[g].data['type']}"
            for p, g, _ in pairs
            if preds[p].data["type"] != golds[g].data["type"]
        ],
        "keyword_tp": len(gold_keywords & pred_keywords),
        "keyword_pred": len(pred_keywords),
        "keyword_gold": len(gold_keywords),
        "keyword_missed": sorted(gold_keywords - pred_keywords),
        "keyword_extra": sorted(pred_keywords - gold_keywords),
        "fields": fields,
    }


def summarize(details: list[dict]) -> dict[str, float | None]:
    def ratio(num: float, den: float) -> float | None:
        return num / den if den else None

    tp = sum(d["keyword_tp"] for d in details)
    precision = ratio(tp, sum(d["keyword_pred"] for d in details))
    recall = ratio(tp, sum(d["keyword_gold"] for d in details))
    fields = [f for d in details for f in d["fields"]]
    gold_empty = [f for f in fields if f[1] is None]
    return {
        "requirement_recall": ratio(
            sum(d["matched"] for d in details), sum(d["gold"] for d in details)
        ),
        "hard_recall": ratio(
            sum(d["matched_hard"] for d in details),
            sum(d["gold_hard"] for d in details),
        ),
        "extra_rate": ratio(
            sum(len(d["extra"]) for d in details), sum(d["pred"] for d in details)
        ),
        "type_accuracy": ratio(
            sum(d["type_ok"] for d in details), sum(d["matched"] for d in details)
        ),
        "quote_verified_rate": ratio(
            sum(d["verified"] for d in details), sum(d["pred"] for d in details)
        ),
        "keyword_precision": precision,
        "keyword_recall": recall,
        "keyword_f1": (
            2 * precision * recall / (precision + recall)
            if precision and recall
            else None
        ),
        "field_accuracy": ratio(sum(f[3] for f in fields), len(fields)),
        "field_invented": ratio(
            sum(f[2] is not None for f in gold_empty), len(gold_empty)
        ),
    }


def render(result: dict[str, Any], runs: list[list[dict]], path: Path) -> str:
    summaries = [summarize(details) for details in runs]
    lines = [
        f"# E2 JD 解析评测：{result['created_at']}",
        "",
        f"- 结果文件：`{path.relative_to(REPO_ROOT)}`",
        f"- 代码：`{result['git']['commit'][:7]}`"
        + ("（有未提交改动）" if result["git"]["dirty"] else ""),
        f"- 提示词：`{result['prompt_version']}`；模型：`{result['model']}`",
        f"- 样本：{', '.join(result['inputs'])}；每份运行 {len(runs)} 次",
        "",
        "## 指标",
        "",
        "| 指标 | 平均 | 最低～最高 | 预期 |",
        "| --- | ---: | ---: | ---: |",
    ]
    for key, label in LABELS.items():
        values = [s[key] for s in summaries if s[key] is not None]
        low_high = f"{pct(min(values))}～{pct(max(values))}" if values else "—"
        expected = f"≥ {EXPECTATIONS[key]:.0%}" if key in EXPECTATIONS else ""
        average = pct(mean(values) if values else None)
        lines.append(f"| {label} | {average} | {low_high} | {expected} |")

    def tally(entries: list[str]) -> list[str]:
        return [
            f"- （{n}/{len(runs)} 次）{text}"
            for text, n in Counter(entries).most_common()
        ]

    every = [d for details in runs for d in details]
    sections = [
        ("漏掉的要求", [m for d in every for m in d["missed"]]),
        (
            "没配上的预测（细拆、不该算要求的内容，还是编造）",
            [e for d in every for e in d["extra"]],
        ),
        (
            "硬性 / 加分分错（预测 → 标注）",
            [t for d in every for t in d["type_errors"]],
        ),
        ("漏掉的关键词", [k for d in every for k in d["keyword_missed"]]),
        ("多出的关键词", [k for d in every for k in d["keyword_extra"]]),
        (
            "基本字段不一致（字段：标注 / 预测）",
            [f"{n}：{e} / {a}" for d in every for n, e, a, ok in d["fields"] if not ok],
        ),
    ]
    for title, entries in sections:
        lines += ["", f"## {title}", ""]
        lines += tally(entries) or ["无"]

    calls = [c for r in result["results"] for c in r["calls"]]
    succeeded = [c for c in calls if c["outcome"] == "success"]
    errors = [r["error"] for r in result["results"] if r["error"]]
    lines += ["", "## 成本与失败", ""]
    if succeeded:
        tokens = mean(c["prompt_tokens"] + c["completion_tokens"] for c in succeeded)
        latency = mean(c["latency_ms"] for c in succeeded) / 1000
        lines.append(
            f"- 调用 {len(calls)} 次，成功 {len(succeeded)} 次；"
            f"成功调用平均 {tokens:.0f} token、{latency:.1f} 秒"
        )
    lines.append(
        f"- 失败 {len(errors)} 次" + ("：" + "；".join(errors) if errors else "")
    )
    return "\n".join(lines) + "\n"


def main(path: Path | None) -> int:
    if path is None:
        found = sorted(RESULT_DIR.glob("e2-*.json"))
        if not found:
            print("evals/results/ 下没有 E2 结果，先运行 e2_parse_jd。")
            return 1
        path = found[-1]
    path = path.resolve()
    result = json.loads(path.read_text(encoding="utf-8"))

    missing = [
        s for s in result["inputs"] if not (ANNOTATION_DIR / f"{s}.json").exists()
    ]
    if missing:
        print(f"这些 JD 还没有标注：{', '.join(missing)}")
        return 1
    golds = {
        s: json.loads((ANNOTATION_DIR / f"{s}.json").read_text(encoding="utf-8"))
        for s in result["inputs"]
    }
    runs = [
        [
            score_record(
                r, result["inputs"][r["sample_id"]]["raw_text"], golds[r["sample_id"]]
            )
            for r in result["results"]
            if r["run_index"] == run_index and not r["error"]
        ]
        for run_index in range(result["runs"])
    ]

    report = render(result, runs, path)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"{path.stem}.md"
    out.write_text(report, encoding="utf-8")
    print(report)
    print(f"报告已写入 {out.relative_to(REPO_ROOT)}（不提交）")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("result", nargs="?", type=Path, help="结果文件，默认最新一份")
    args = parser.parse_args()
    sys.exit(main(args.result))
