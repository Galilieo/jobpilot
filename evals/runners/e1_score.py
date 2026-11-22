"""E1 第二步：把运行结果与标注对照计分，汇总写入 evals/reports/。

不调用模型，可以反复运行。在仓库根目录运行：

    uv run --project jobpilot python -m evals.runners.e1_score
    uv run --project jobpilot python -m evals.runners.e1_score evals/results/e1-xxx.json

口径见 04 第 3 节 E1 与第 1.2 节的预期。配对分两步：原文位置有重叠才是候选，
候选中按文字相似度做一对一配对，使总相似度最大。
"""

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from statistics import mean
from typing import Any

from jobpilot.config import REPO_ROOT
from jobpilot.services.facts import locate_quote, numbers_missing_from_quote

RESULT_DIR = REPO_ROOT / "evals" / "results"
REPORT_DIR = REPO_ROOT / "evals" / "reports"
ANNOTATION_DIR = REPO_ROOT / "evals" / "datasets" / "annotations"

# 相似度低于它的候选不配对：位置重叠但说的不是一件事。
MIN_SIMILARITY = 0.3
# 相似度低于它的配对列出来，由人工复核是否配对正确。
REVIEW_SIMILARITY = 0.6
# 一个连通块里预测条数超过它时改用贪心，避免穷举过慢。开发集远小于这个数。
EXHAUSTIVE_LIMIT = 8

RANK = {"participate": 0, "own": 1, "lead": 2}

# 04 第 1.2 节的初始预期。
EXPECTATIONS = {
    "recall": 0.85,
    "verified_rate": 0.95,
    "flag_recall": 0.90,
    "flag_precision": 0.50,
}


@dataclass
class Item:
    """一条事实及其原文位置；预测的片段没通过校验时位置为空。"""

    data: dict[str, Any]
    start: int | None
    end: int | None

    @property
    def text(self) -> str:
        return self.data["text"]


def overlaps(a: Item, b: Item) -> bool:
    if a.start is None or b.start is None:
        return False
    assert a.end is not None and b.end is not None
    return a.start < b.end and b.start < a.end


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


# ---------- 配对 ----------


def match(preds: list[Item], golds: list[Item]) -> list[tuple[int, int, float]]:
    """一对一配对，返回 (预测下标, 标注下标, 相似度)。

    贪心按分数从高到低选，可能为了一个高分对牺牲另一个，总分不是最优；
    所以先按候选关系分成互不相连的小块，每块内穷举，找总相似度最大的配法。
    """
    scores = {
        (p, g): similarity(pred.text, gold.text)
        for p, pred in enumerate(preds)
        for g, gold in enumerate(golds)
        if overlaps(pred, gold)
    }
    scores = {pair: s for pair, s in scores.items() if s >= MIN_SIMILARITY}

    pairs: list[tuple[int, int, float]] = []
    for block_preds, block_golds in components(scores):
        chosen = (
            best_assignment(block_preds, scores)
            if len(block_preds) <= EXHAUSTIVE_LIMIT
            else greedy_assignment(block_preds, block_golds, scores)
        )
        pairs.extend((p, g, scores[(p, g)]) for p, g in chosen)
    return sorted(pairs)


def components(
    scores: dict[tuple[int, int], float],
) -> list[tuple[list[int], set[int]]]:
    """把候选关系看成二分图，返回各连通块的预测下标和标注下标。"""
    parent: dict[tuple[str, int], tuple[str, int]] = {}

    def find(node: tuple[str, int]) -> tuple[str, int]:
        parent.setdefault(node, node)
        while parent[node] != node:
            node = parent[node]
        return node

    for p, g in scores:
        parent[find(("p", p))] = find(("g", g))

    blocks: dict[tuple[str, int], tuple[list[int], set[int]]] = {}
    for p, g in scores:
        block = blocks.setdefault(find(("p", p)), ([], set()))
        if p not in block[0]:
            block[0].append(p)
        block[1].add(g)
    return list(blocks.values())


def best_assignment(
    preds: list[int], scores: dict[tuple[int, int], float]
) -> list[tuple[int, int]]:
    """逐个预测决定配哪条标注或不配，穷举所有组合取总分最高的。"""
    best: tuple[float, list[tuple[int, int]]] = (0.0, [])

    def search(i: int, used: set[int], total: float, chosen: list[tuple[int, int]]):
        nonlocal best
        if i == len(preds):
            if total > best[0]:
                best = (total, list(chosen))
            return
        p = preds[i]
        search(i + 1, used, total, chosen)  # 这条预测不配
        for (pp, g), s in scores.items():
            if pp == p and g not in used:
                chosen.append((p, g))
                search(i + 1, used | {g}, total + s, chosen)
                chosen.pop()

    search(0, set(), 0.0, [])
    return best[1]


def greedy_assignment(
    preds: list[int], golds: set[int], scores: dict[tuple[int, int], float]
) -> list[tuple[int, int]]:
    candidates = sorted(
        ((s, p, g) for (p, g), s in scores.items() if p in preds and g in golds),
        reverse=True,
    )
    used_p: set[int] = set()
    used_g: set[int] = set()
    chosen = []
    for _, p, g in candidates:
        if p not in used_p and g not in used_g:
            chosen.append((p, g))
            used_p.add(p)
            used_g.add(g)
    return chosen


# ---------- 计分 ----------


def load_golds(sample_id: str, raw_text: str) -> list[Item]:
    path = ANNOTATION_DIR / f"{sample_id}.json"
    facts = json.loads(path.read_text(encoding="utf-8"))["facts"]
    items = []
    for fact in facts:
        [(start, end)] = locate_quote(
            raw_text, fact["source_quote"]
        )  # 标注自检保证唯一
        items.append(Item(fact, start, end))
    return items


def score_record(record: dict[str, Any], raw_text: str) -> dict[str, Any]:
    """一次运行中一份简历的计分明细。"""
    golds = load_golds(record["sample_id"], raw_text)
    preds = [Item(f, f["span_start"], f["span_end"]) for f in record["facts"]]
    pairs = match(preds, golds)
    matched_p = {p for p, _, _ in pairs}
    matched_g = {g for _, g, _ in pairs}

    detail: dict[str, Any] = {
        "gold": len(golds),
        "pred": len(preds),
        "verified": sum(1 for p in preds if p.data["quote_verified"]),
        "matched": len(pairs),
        "missed": [golds[g].text for g in range(len(golds)) if g not in matched_g],
        "extra": [
            {"text": preds[p].text, "verified": preds[p].data["quote_verified"]}
            for p in range(len(preds))
            if p not in matched_p
        ],
        "pairs": [],
    }

    # 核对原因：程序能判断的「确实有问题」——片段未通过、职责夸大、数字不对、
    # 标注要求确认。没配上且片段通过的预测是否有问题要人工看，不计入。
    flag_rows = [
        (True, bool(p.data["review_reasons"]), p.data["review_reasons"], p.text, True)
        for p in preds
        if not p.data["quote_verified"]
    ]
    for p, g, s in pairs:
        pred, gold = preds[p].data, golds[g].data
        exaggerated = RANK.get(pred["responsibility"], -1) > RANK.get(
            gold["responsibility"], -1
        )
        understated = RANK.get(pred["responsibility"], -1) < RANK.get(
            gold["responsibility"], -1
        )
        # 拆得比标注粗时，预测里会带上别的标注的数字；只要数字在已通过校验的
        # 预测片段里，它就来自原文，不算错。
        evidence = [gold["text"], gold["source_quote"]]
        if pred["quote_verified"]:
            evidence.append(pred["source_quote"])
        wrong_numbers = numbers_missing_from_quote(pred["text"], " ".join(evidence))
        problem = exaggerated or bool(wrong_numbers) or gold["needs_confirmation"]
        flag_rows.append(
            (
                problem,
                bool(pred["review_reasons"]),
                pred["review_reasons"],
                pred["text"],
                exaggerated or bool(wrong_numbers),  # 会导致夸大或数字错误的问题
            )
        )
        detail["pairs"].append(
            {
                "pred": pred["text"],
                "gold": gold["text"],
                "similarity": round(s, 2),
                "kind": (pred["kind"], gold["kind"]),
                "kind_ok": pred["kind"] == gold["kind"],
                "responsibility": (pred["responsibility"], gold["responsibility"]),
                "exaggerated": exaggerated,
                "understated": understated,
                "has_number": (pred["has_number"], gold["has_number"]),
                "has_number_ok": pred["has_number"] == gold["has_number"],
                "wrong_numbers": wrong_numbers,
                "needs_confirmation": gold["needs_confirmation"],
                "reasons": pred["review_reasons"],
            }
        )
    detail["flags"] = flag_rows
    return detail


def summarize(details: list[dict[str, Any]]) -> dict[str, float | None]:
    """一次运行（全部简历）的指标。"""
    gold = sum(d["gold"] for d in details)
    pred = sum(d["pred"] for d in details)
    pairs = [pair for d in details for pair in d["pairs"]]
    flags = [row for d in details for row in d["flags"]]
    problems = [row for row in flags if row[0]]
    flagged = [row for row in flags if row[1]]
    risks = [row for row in flags if row[4]]
    unstated = [p for p in pairs if p["needs_confirmation"]]

    def ratio(num: int, den: int) -> float | None:
        return num / den if den else None

    return {
        "recall": ratio(sum(d["matched"] for d in details), gold),
        "verified_rate": ratio(sum(d["verified"] for d in details), pred),
        "extra_rate": ratio(sum(len(d["extra"]) for d in details), pred),
        "kind_acc": ratio(sum(p["kind_ok"] for p in pairs), len(pairs)),
        "responsibility_acc": ratio(
            sum(p["responsibility"][0] == p["responsibility"][1] for p in pairs),
            len(pairs),
        ),
        "exaggerated": ratio(sum(p["exaggerated"] for p in pairs), len(pairs)),
        "understated": ratio(sum(p["understated"] for p in pairs), len(pairs)),
        "has_number_acc": ratio(sum(p["has_number_ok"] for p in pairs), len(pairs)),
        "wrong_number": ratio(sum(bool(p["wrong_numbers"]) for p in pairs), len(pairs)),
        "flag_recall": ratio(sum(row[1] for row in problems), len(problems)),
        "flag_precision": ratio(sum(row[0] for row in flagged), len(flagged)),
        "risk_flag_recall": ratio(sum(row[1] for row in risks), len(risks)),
        "unstated_recall": ratio(
            sum("responsibility_unstated" in p["reasons"] for p in unstated),
            len(unstated),
        ),
    }


# ---------- 报告 ----------

LABELS = {
    "recall": "事实召回率",
    "verified_rate": "原文片段校验通过率",
    "extra_rate": "没配上的预测占比",
    "kind_acc": "类型准确率",
    "responsibility_acc": "职责准确率",
    "exaggerated": "职责夸大率",
    "understated": "职责低估率",
    "has_number_acc": "含数字判断准确率",
    "wrong_number": "数字错误率",
    "flag_recall": "核对原因召回率",
    "flag_precision": "核对原因精确率",
    "risk_flag_recall": "其中：夸大与数字问题的标出率",
    "unstated_recall": "其中：职责未写明的提示率",
}


def pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def render(result: dict[str, Any], runs: list[list[dict[str, Any]]]) -> str:
    summaries = [summarize(details) for details in runs]
    lines = [
        f"# E1 事实拆分评测：{result['created_at']}",
        "",
        f"- 结果文件：`{result['_path']}`",
        f"- 代码：`{result['git']['commit'][:7]}`"
        + ("（有未提交改动）" if result["git"]["dirty"] else ""),
        f"- 提示词：`{result['prompt_version']}`；模型：`{result['model']}`",
        f"- 样本：{', '.join(result['inputs'])}；每份运行 {len(runs)} 次",
        f"- 标注：{sum(d['gold'] for d in runs[0])} 条（Claude 初标、本人复核）",
        "",
        "## 指标",
        "",
        "| 指标 | 平均 | 最低～最高 | 预期 |",
        "| --- | ---: | ---: | ---: |",
    ]
    for key, label in LABELS.items():
        values = [s[key] for s in summaries if s[key] is not None]
        avg = mean(values) if values else None
        low_high = f"{pct(min(values))}～{pct(max(values))}" if values else "—"
        expected = f"≥ {EXPECTATIONS[key]:.0%}" if key in EXPECTATIONS else ""
        lines.append(f"| {label} | {pct(avg)} | {low_high} | {expected} |")

    def tally(entries: list[str]) -> list[str]:
        counts = Counter(entries)
        return [f"- （{n}/{len(runs)} 次）{text}" for text, n in counts.most_common()]

    all_details = [d for details in runs for d in details]
    sections = [
        ("漏拆的标注", [m for d in all_details for m in d["missed"]]),
        (
            "没配上的预测（人工判断：细拆、不算事实的内容，还是编造）",
            [
                e["text"] + ("" if e["verified"] else "【片段未通过】")
                for d in all_details
                for e in d["extra"]
            ],
        ),
        (
            f"相似度低于 {REVIEW_SIMILARITY} 的配对（人工复核配得对不对）",
            [
                f"{p['pred']} ↔ {p['gold']}（{p['similarity']}）"
                for d in all_details
                for p in d["pairs"]
                if p["similarity"] < REVIEW_SIMILARITY
            ],
        ),
        (
            "职责与标注不同（模型 → 标注）",
            [
                f"{p['pred']}：{p['responsibility'][0]} → {p['responsibility'][1]}"
                for d in all_details
                for p in d["pairs"]
                if p["responsibility"][0] != p["responsibility"][1]
            ],
        ),
        (
            "类型与标注不同（模型 → 标注）",
            [
                f"{p['pred']}：{p['kind'][0]} → {p['kind'][1]}"
                for d in all_details
                for p in d["pairs"]
                if not p["kind_ok"]
            ],
        ),
        (
            "含数字判断与标注不同（模型 → 标注）",
            [
                f"{p['pred']}：{p['has_number'][0]} → {p['has_number'][1]}"
                for d in all_details
                for p in d["pairs"]
                if not p["has_number_ok"]
            ],
        ),
        (
            "数字与标注不符",
            [
                f"{p['pred']}：{p['wrong_numbers']}"
                for d in all_details
                for p in d["pairs"]
                if p["wrong_numbers"]
            ],
        ),
        (
            "有问题但没标出（漏标）",
            [
                row[3]
                for d in all_details
                for row in d["flags"]
                if row[0] and not row[1]
            ],
        ),
        (
            "标出了但程序判断没问题（可能误标，原因见括号）",
            [
                f"{row[3]}（{', '.join(row[2])}）"
                for d in all_details
                for row in d["flags"]
                if row[1] and not row[0]
            ],
        ),
    ]
    for title, entries in sections:
        lines += ["", f"## {title}", ""]
        lines += tally(entries) or ["无"]

    calls = [c for records in result["_records"] for c in records["calls"]]
    # 超时或连接失败的调用没有用量，只计入次数，平均值按成功的调用算。
    succeeded = [c for c in calls if c["outcome"] == "success"]
    tokens = [c["prompt_tokens"] + c["completion_tokens"] for c in succeeded]
    latency = [c["latency_ms"] for c in succeeded]
    others = Counter(c["outcome"] for c in calls if c["outcome"] != "success")
    errors = [r["error"] for r in result["_records"] if r["error"]]
    lines += [
        "",
        "## 成本与失败",
        "",
        f"- 调用 {len(calls)} 次，成功 {len(succeeded)} 次；成功调用平均 "
        f"{mean(tokens):.0f} token、{mean(latency) / 1000:.1f} 秒",
        "- 未成功的调用："
        + ("、".join(f"{k} {v} 次" for k, v in others.items()) if others else "无"),
        f"- 失败 {len(errors)} 次" + ("：" + "；".join(errors) if errors else ""),
    ]
    return "\n".join(lines) + "\n"


def main(path: Path | None) -> int:
    if path is None:
        found = sorted(RESULT_DIR.glob("e1-*.json"))
        if not found:
            print("evals/results/ 下没有 E1 结果，先运行 e1_extract_facts。")
            return 1
        path = found[-1]
    path = path.resolve()  # 命令行传入的可能是相对路径
    result = json.loads(path.read_text(encoding="utf-8"))
    result["_path"] = str(path.relative_to(REPO_ROOT))
    result["_records"] = result["results"]

    runs: list[list[dict[str, Any]]] = []
    for run_index in range(result["runs"]):
        runs.append(
            [
                score_record(record, result["inputs"][record["sample_id"]]["raw_text"])
                for record in result["results"]
                if record["run_index"] == run_index and not record["error"]
            ]
        )

    report = render(result, runs)
    REPORT_DIR.mkdir(exist_ok=True)
    out = REPORT_DIR / f"{path.stem}.md"
    out.write_text(report, encoding="utf-8")
    print(report)
    print(f"报告已写入 {out.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("result", nargs="?", type=Path, help="结果文件，默认最新一份")
    args = parser.parse_args()
    sys.exit(main(args.result))
