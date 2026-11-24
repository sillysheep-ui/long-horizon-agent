"""两次评测的逐用例配对对比：均值差、bootstrap 区间与改善/回退计数。"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any


def load_records(path: str | Path) -> list[dict[str, Any]]:
    """读取 records.json，或目录下的 records.json。"""
    target = Path(path)
    if target.is_dir():
        target = target / "records.json"
    return json.loads(target.read_text(encoding="utf-8"))


def _metric_value(record: dict[str, Any], metric: str) -> Any:
    if metric == "score":
        return record.get("score")
    if metric == "hard_pass":
        checks = record.get("checks") or {}
        return bool(checks) and all(checks.values())
    raise ValueError(f"未知指标：{metric}")


def paired_rows(
    rows_a: list[dict[str, Any]],
    rows_b: list[dict[str, Any]],
    metric: str = "score",
) -> list[tuple[str, Any, Any]]:
    """按 id 对齐，只保留两版都有该指标值的用例。"""
    left = {str(r.get("id")): _metric_value(r, metric) for r in rows_a}
    right = {str(r.get("id")): _metric_value(r, metric) for r in rows_b}
    pairs = []
    for key in sorted(set(left) & set(right)):
        a, b = left[key], right[key]
        if a is None or b is None:
            continue
        pairs.append((key, a, b))
    return pairs


def bootstrap_ci(values: list[float], n: int = 2000, seed: int = 7) -> tuple[float, float]:
    """均值的 95% bootstrap 区间。"""
    if not values:
        return (0.0, 0.0)
    rng = random.Random(seed)
    means = []
    for _ in range(n):
        sample = [values[rng.randrange(len(values))] for _ in values]
        means.append(sum(sample) / len(sample))
    means.sort()
    return (means[int(0.025 * n)], means[int(0.975 * n) - 1])


def summarize_pairs(pairs: list[tuple[str, Any, Any]], metric: str = "score") -> dict[str, Any]:
    """汇总配对差：均值、区间、改善/回退。"""
    diffs = [float(b) - float(a) for _, a, b in pairs]
    improved = sum(1 for d in diffs if d > 0)
    regressed = sum(1 for d in diffs if d < 0)
    same = len(diffs) - improved - regressed
    low, high = bootstrap_ci(diffs)
    mean = sum(diffs) / len(diffs) if diffs else 0.0
    return {
        "metric": metric,
        "cases": len(pairs),
        "mean_diff": round(mean, 4),
        "ci_low": round(low, 4),
        "ci_high": round(high, 4),
        "improved": improved,
        "regressed": regressed,
        "same": same,
        "changed_ids": [key for key, a, b in pairs if a != b],
    }


def markdown(a_name: str, b_name: str, summary: dict[str, Any], limit: int = 15) -> str:
    """生成对比报告。"""
    lines = [
        f"# 对比：{a_name} → {b_name}",
        "",
        f"- 指标：{summary['metric']}",
        f"- 配对用例：{summary['cases']}",
        f"- 均值差：{summary['mean_diff']:+.4f}（95% 区间 {summary['ci_low']:+.4f} ~ {summary['ci_high']:+.4f}）",
        f"- 改善 / 回退 / 不变：{summary['improved']} / {summary['regressed']} / {summary['same']}",
        "",
        "## 变化用例",
        "",
    ]
    for key in summary["changed_ids"][:limit]:
        lines.append(f"- {key}")
    if not summary["changed_ids"]:
        lines.append("- 无")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="配对比较两次评测")
    parser.add_argument("--a", required=True, help="旧版结果目录或 records.json")
    parser.add_argument("--b", required=True, help="新版结果目录或 records.json")
    parser.add_argument("--metric", choices=("score", "hard_pass"), default="score")
    parser.add_argument("--out", default="", help="报告输出路径")
    args = parser.parse_args(argv)

    pairs = paired_rows(load_records(args.a), load_records(args.b), args.metric)
    if not pairs:
        raise SystemExit("没有可配对的用例")
    summary = summarize_pairs(pairs, args.metric)
    report = markdown(Path(args.a).name, Path(args.b).name, summary)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"报告：{args.out}")
    print(json.dumps({k: v for k, v in summary.items() if k != "changed_ids"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
