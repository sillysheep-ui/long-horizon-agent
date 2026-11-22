"""联合报告：把各个 suite 的 report.json 合成一张表。

用法：
    python3 -m evaluation.combined results/a/report.json results/b/report.json
    python3 -m evaluation.combined --dir results/product_eval        # 找最近一份
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ORDER = ("single_turn", "sandbox", "revision", "intent")


def load(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def latest_reports(root: Path) -> list[Path]:
    """在目录下按 suite 取最近修改的 report.json。"""
    found: dict[str, tuple[float, Path]] = {}
    for path in root.rglob("report.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        suite = str(payload.get("suite") or "")
        if not suite:
            continue
        stamp = path.stat().st_mtime
        if suite not in found or stamp > found[suite][0]:
            found[suite] = (stamp, path)
    return [path for _, path in sorted(found.values(), key=lambda item: -item[0])]


def rows(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(reports, key=lambda item: ORDER.index(item.get("suite", ""))
                     if item.get("suite") in ORDER else len(ORDER))
    table: list[dict[str, Any]] = []
    for report in ordered:
        metrics = report.get("metrics") or {}
        interval = metrics.get("score_ci95")
        table.append({
            "suite": report.get("suite"),
            "cases": report.get("cases"),
            "main_metric": report.get("main_metric"),
            "value": report.get("main_value"),
            "ci95": interval,
            "dataset": report.get("dataset_version"),
            "evaluator": report.get("evaluator_version"),
            "comparable": bool((report.get("labels") or {}).get("comparable")),
        })
    return table


def markdown(table: list[dict[str, Any]]) -> str:
    lines = ["# 联合评测报告", "",
             "| suite | cases | 主指标 | 值 | 95% 区间 | 数据版本 | 评测器 |",
             "|---|---|---|---|---|---|---|"]
    for row in table:
        interval = row["ci95"]
        shown = f"[{interval[0]}, {interval[1]}]" if interval else "—"
        lines.append(f"| {row['suite']} | {row['cases']} | {row['main_metric']} | {row['value']} "
                     f"| {shown} | {row['dataset']} | {row['evaluator']} |")
    lines += ["", "> 各 suite 的主指标不可横向比较：单轮看综合分，沙盒看硬约束通过率，"
                  "多轮看整条用例是否全过，意图看分类准确率。"]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("reports", nargs="*", help="report.json 路径")
    parser.add_argument("--dir", default="results/product_eval", help="从目录里找各 suite 最近一份")
    parser.add_argument("--out", default="", help="输出 markdown 路径")
    args = parser.parse_args()

    paths = [Path(item) for item in args.reports] or latest_reports(Path(args.dir))
    if not paths:
        raise SystemExit("没有找到 report.json")
    table = rows([load(path) for path in paths])
    text = markdown(table)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"联合报告：{args.out}")
    print(text)


if __name__ == "__main__":
    main()
