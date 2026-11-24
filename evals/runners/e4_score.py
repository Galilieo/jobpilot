"""E4 第二步：按三种组合给依据检查计分。不调用模型，可以反复运行。

    uv run --project jobpilot python -m evals.runners.e4_score
    uv run --project jobpilot python -m evals.runners.e4_score evals/results/e4-dev-xxx.json
    uv run --project jobpilot python -m evals.runners.e4_score --recheck

--recheck：第一、二层按当前代码重算，第三层沿用结果文件里的模型输出，不花钱。
改了程序规则之后用它对比前后效果；报告文件名带 -recheck，并记下重算时的代码版本。

组合：只用第一层；第一 + 二层；第一 + 二 + 三层；另列第三层单独的结果作参考。
第三层出错或输出不合格时按拦下计（与产品里的 fail-closed 一致），错误次数单独列出。
评测数据是合成的，报告不含真实简历，写入 evals/reports/，可以提交。
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean
from typing import Any

from jobpilot.config import REPO_ROOT

RESULT_DIR = REPO_ROOT / "evals" / "results"
REPORT_DIR = REPO_ROOT / "evals" / "reports"

NORMAL = "normal"
LABELS = {
    "technology": "凭空添加技术",
    "number": "数字编造或放大",
    "role": "角色升级",
    "degree": "程度升级",
    "cross_experience": "张冠李戴",
    "basic_info": "基本信息改动",
    "implicit": "隐含夸大",
}
CONFIGS = {
    "L1": "只用第一层",
    "L12": "第一 + 二层",
    "L123": "第一 + 二 + 三层",
    "L3": "（参考）只用第三层",
}
# 04 第 1.2 节的预期。
EXPECTED_DETECTION = 0.90
EXPECTED_FALSE_BLOCK = 0.10


def blocked(case: dict[str, Any], semantic: dict[str, Any]) -> dict[str, bool]:
    l1 = bool(case["layer1"])
    l2 = bool(case["layer2"])
    l3 = bool(semantic["error"] or semantic["issues"])
    return {"L1": l1, "L12": l1 or l2, "L123": l1 or l2 or l3, "L3": l3}


def score_run(cases: dict[str, dict], semantic: list[dict]) -> dict[str, Any]:
    by_config: dict[str, dict[str, list[bool]]] = defaultdict(lambda: defaultdict(list))
    first_layer: Counter[str] = Counter()
    for record in semantic:
        case = cases[record["case_id"]]
        outcome = blocked(case, record)
        for config, value in outcome.items():
            by_config[config][case["label"]].append(value)
        if case["label"] != NORMAL:
            if case["layer1"]:
                first_layer["第一层"] += 1
            elif case["layer2"]:
                first_layer["第二层"] += 1
            elif outcome["L3"]:
                first_layer["第三层"] += 1
            else:
                first_layer["都没拦住"] += 1

    def rate(values: list[bool]) -> float | None:
        return sum(values) / len(values) if values else None

    summary = {}
    for config, labels in by_config.items():
        fabricated = [v for label, vs in labels.items() if label != NORMAL for v in vs]
        summary[config] = {
            "detection": rate(fabricated),
            "false_block": rate(labels[NORMAL]),
            "by_label": {label: rate(labels[label]) for label in LABELS},
        }
    return {"summary": summary, "first_layer": first_layer}


def pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def spread(values: list[float | None]) -> str:
    present = [v for v in values if v is not None]
    if not present:
        return "—"
    if len(set(present)) == 1:
        return pct(present[0])
    return f"{pct(mean(present))}（{pct(min(present))}～{pct(max(present))}）"


def render(result: dict[str, Any], runs: list[dict[str, Any]], path: Path) -> str:
    cases = {case["case_id"]: case for case in result["cases"]}
    counts = Counter(case["label"] for case in result["cases"])
    lines = [
        f"# E4 依据检查评测：{result['split']}，{result['created_at']}",
        "",
        f"- 结果文件：`{path.relative_to(REPO_ROOT)}`",
        f"- 代码：`{result['git']['commit'][:7]}`"
        + ("（有未提交改动）" if result["git"]["dirty"] else ""),
        f"- 第三层提示词：`{result['prompt_version']}`；模型：`{result['model']}`",
        *(
            [
                f"- 第一、二层按 `{result['recheck_git']['commit'][:7]}`"
                + ("（有未提交改动）" if result["recheck_git"]["dirty"] else "")
                + " 的代码重算，第三层沿用原结果"
            ]
            if "recheck_git" in result
            else []
        ),
        (
            f"- 句子：{len(cases)} 句（正常 {counts[NORMAL]}、编造 "
            f"{len(cases) - counts[NORMAL]}）；第三层每句 {result['runs']} 次"
        ),
        "",
        "## 三种组合",
        "",
        (
            f"| 组合 | 编造检出率（预期 ≥ {pct(EXPECTED_DETECTION)}） "
            f"| 误拦率（预期 ≤ {pct(EXPECTED_FALSE_BLOCK)}） |"
        ),
        "| --- | ---: | ---: |",
    ]
    for config, name in CONFIGS.items():
        detection = [run["summary"][config]["detection"] for run in runs]
        false_block = [run["summary"][config]["false_block"] for run in runs]
        lines.append(f"| {name} | {spread(detection)} | {spread(false_block)} |")

    lines += [
        "",
        "## 按编造类型的检出率",
        "",
        "| 类型 | 句数 | "
        + " | ".join(CONFIGS[c] for c in ("L1", "L12", "L123"))
        + " |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for label, name in LABELS.items():
        cells = [
            spread([run["summary"][c]["by_label"][label] for run in runs])
            for c in ("L1", "L12", "L123")
        ]
        lines.append(f"| {name} | {counts[label]} | " + " | ".join(cells) + " |")

    lines += ["", "## 编造句最先被哪一层拦下（各次合计）", ""]
    total: Counter[str] = Counter()
    for run in runs:
        total.update(run["first_layer"])
    lines += [f"- {layer}：{count}" for layer, count in total.most_common()]

    # 失败案例：完整组合下漏掉的编造、被拦下的正常句。
    misses: dict[str, int] = Counter()
    false_blocks: Counter[str] = Counter()
    reasons_by_case: dict[str, Counter[str]] = defaultdict(Counter)
    for record in result["semantic"]:
        case = cases[record["case_id"]]
        outcome = blocked(case, record)
        if case["label"] != NORMAL and not outcome["L123"]:
            misses[case["case_id"]] += 1
        if case["label"] == NORMAL and outcome["L123"]:
            reasons = [i["message"] for i in case["layer1"] + case["layer2"]]
            reasons += [i["message"] for i in record["issues"]]
            if record["error"]:
                reasons.append(f"第三层出错：{record['error']}")
            false_blocks[case["case_id"]] += 1
            reasons_by_case[case["case_id"]].update(set(reasons))

    lines += ["", "## 漏掉的编造（完整组合）", ""]
    for case_id, times in sorted(misses.items()):
        case = cases[case_id]
        lines.append(
            f"- （{times}/{result['runs']} 次）`{case_id}` {LABELS[case['label']]}："
            f"{case['sentence']}　— {case.get('note', '')}"
        )
    if not misses:
        lines.append("无")

    lines += ["", "## 被误拦的正常句（完整组合）", ""]
    for case_id, times in sorted(false_blocks.items()):
        reasons = "；".join(
            f"{reason}（{count} 次）"
            for reason, count in sorted(reasons_by_case[case_id].items())
        )
        lines.append(
            f"- （{times}/{result['runs']} 次）`{case_id}` "
            f"{cases[case_id]['sentence']}　— {reasons}"
        )
    if not false_blocks:
        lines.append("无")

    errors = [r for r in result["semantic"] if r["error"]]
    calls = [c for r in result["semantic"] for c in r["calls"]]
    succeeded = [c for c in calls if c["outcome"] == "success"]
    lines += ["", "## 成本与失败", ""]
    if succeeded:
        tokens = mean(c["prompt_tokens"] + c["completion_tokens"] for c in succeeded)
        latency = mean(c["latency_ms"] for c in succeeded) / 1000
        lines.append(
            f"- 第三层调用 {len(calls)} 次，成功 {len(succeeded)} 次；"
            f"成功调用平均 {tokens:.0f} token、{latency:.1f} 秒"
        )
    unavailable = sum(
        1
        for r in result["semantic"]
        for i in r["issues"]
        if i["kind"] == "check_unavailable"
    )
    lines.append(f"- 运行失败 {len(errors)} 次；检查未完成（按拦下计）{unavailable} 次")
    return "\n".join(lines) + "\n"


def recheck(result: dict[str, Any]) -> None:
    """按当前代码重算每句的第一、二层，原地替换。"""
    from evals.runners.e1_extract_facts import git_state
    from evals.runners.e4_ground_check import load_fact_set, program_layers

    fact_sets: dict[str, Any] = {}
    for case in result["cases"]:
        sample_id = case["sample_id"]
        if sample_id not in fact_sets:
            fact_sets[sample_id] = load_fact_set(sample_id)
        case.update(program_layers(case, fact_sets[sample_id]))
    result["recheck_git"] = git_state()


def main(path: Path | None, *, recheck_program: bool = False) -> int:
    if path is None:
        found = sorted(RESULT_DIR.glob("e4-*.json"))
        if not found:
            print("evals/results/ 下没有 E4 结果，先运行 e4_ground_check。")
            return 1
        path = found[-1]
    path = path.resolve()
    result = json.loads(path.read_text(encoding="utf-8"))
    if recheck_program:
        recheck(result)
    cases = {case["case_id"]: case for case in result["cases"]}
    runs = [
        score_run(cases, [r for r in result["semantic"] if r["run_index"] == index])
        for index in range(result["runs"])
    ]
    report = render(result, runs, path)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"{path.stem}{'-recheck' if recheck_program else ''}.md"
    out.write_text(report, encoding="utf-8")
    print(report)
    print(f"报告已写入 {out.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("result", nargs="?", type=Path, help="结果文件，默认最新一份")
    parser.add_argument(
        "--recheck", action="store_true", help="第一、二层按当前代码重算"
    )
    args = parser.parse_args()
    sys.exit(main(args.result, recheck_program=args.recheck))
