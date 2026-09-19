# -*- coding: utf-8 -*-
"""ReAct 与 Tool DAG 基线记录的无外部依赖汇总逻辑。"""

from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path
from statistics import mean
from typing import Any, Dict, Iterable, List, Optional


def percentile(values: Iterable[float], q: float) -> Optional[float]:
    ordered = sorted(float(v) for v in values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _round(value: Optional[float], digits: int = 2) -> Optional[float]:
    return None if value is None else round(value, digits)


def summarize_records(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(records)
    if total == 0:
        return {"samples": 0}

    latencies = [float(r["total_latency_ms"]) for r in records if r.get("total_latency_ms") is not None]
    tool_latencies = [float(r["tool_latency_ms"]) for r in records if r.get("tool_latency_ms") is not None]
    calls = [int(r.get("tool_calls", 0)) for r in records]
    successful = [r for r in records if r.get("success")]
    answered = [r for r in records if str(r.get("prediction", "")).strip()]
    error_types = Counter(str(r.get("error_type")) for r in records if r.get("error_type"))
    modes = Counter(str(r.get("mode", "unknown")) for r in records)
    plan_records = [r for r in records if r.get("mode") == "tool_dag"]
    valid_plans = [r for r in plan_records if r.get("plan_valid")]
    repaired_plans = [r for r in valid_plans if r.get("plan_repaired")]
    parallel_widths = [float(r["average_parallel_width"]) for r in valid_plans if r.get("average_parallel_width")]
    speedup_proxies = [float(r["parallel_speedup_proxy"]) for r in valid_plans if r.get("parallel_speedup_proxy")]

    return {
        "samples": total,
        "modes": dict(modes),
        "success_rate": round(len(successful) / total, 4),
        "answer_rate": round(len(answered) / total, 4),
        "plan_valid_rate": (
            round(len(valid_plans) / len(plan_records), 4) if plan_records else None
        ),
        "plan_repair_rate": (
            round(len(repaired_plans) / len(valid_plans), 4) if valid_plans else None
        ),
        "avg_tool_calls": _round(mean(calls)),
        "latency_ms": {
            "mean": _round(mean(latencies)) if latencies else None,
            "p50": _round(percentile(latencies, 0.50)),
            "p95": _round(percentile(latencies, 0.95)),
        },
        "tool_latency_ms": {
            "mean": _round(mean(tool_latencies)) if tool_latencies else None,
            "p50": _round(percentile(tool_latencies, 0.50)),
            "p95": _round(percentile(tool_latencies, 0.95)),
        },
        "avg_parallel_width": _round(mean(parallel_widths)) if parallel_widths else None,
        "avg_parallel_speedup_proxy": _round(mean(speedup_proxies)) if speedup_proxies else None,
        "errors": dict(error_types),
    }


def load_jsonl(path: str) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    with open(path, encoding="utf-8") as file:
        for line in file:
            if line.strip():
                records.append(json.loads(line))
    return records


def _fmt(value: Any, suffix: str = "") -> str:
    if value is None:
        return "N/A"
    return f"{value}{suffix}"


def build_markdown_report(
    react_summary: Optional[Dict[str, Any]],
    dag_summary: Optional[Dict[str, Any]],
) -> str:
    lines = [
        "# ReAct vs Tool DAG 基线测评",
        "",
        "> 本报告的成功率为系统执行成功率，不等同于答案质量。答案质量应另行使用双向 LLM Judge 或人工评测。",
        "",
        "| 指标 | ReAct | Tool DAG |",
        "|---|---:|---:|",
    ]

    def get(summary: Optional[Dict[str, Any]], *keys: str) -> Any:
        current: Any = summary
        for key in keys:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        return current

    rows = [
        ("样本数", ("samples",), ""),
        ("执行成功率", ("success_rate",), ""),
        ("有效回答率", ("answer_rate",), ""),
        ("DAG 合法率", ("plan_valid_rate",), ""),
        ("DAG 自动修复率", ("plan_repair_rate",), ""),
        ("平均工具调用数", ("avg_tool_calls",), ""),
        ("端到端延迟 P50", ("latency_ms", "p50"), " ms"),
        ("端到端延迟 P95", ("latency_ms", "p95"), " ms"),
        ("工具阶段延迟 P50", ("tool_latency_ms", "p50"), " ms"),
        ("平均并行宽度", ("avg_parallel_width",), ""),
        ("并行加速代理值", ("avg_parallel_speedup_proxy",), "×"),
    ]
    for label, keys, suffix in rows:
        lines.append(
            f"| {label} | {_fmt(get(react_summary, *keys), suffix)} | "
            f"{_fmt(get(dag_summary, *keys), suffix)} |"
        )

    lines.extend(["", "## 错误分布", ""])
    for name, summary in (("ReAct", react_summary), ("Tool DAG", dag_summary)):
        errors = get(summary, "errors") or {}
        rendered = ", ".join(f"{key}: {value}" for key, value in errors.items()) or "无"
        lines.append(f"- {name}：{rendered}")
    lines.append("")
    return "\n".join(lines)


def write_summary_files(
    output_dir: str,
    react_records: Optional[List[Dict[str, Any]]] = None,
    dag_records: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    react_summary = summarize_records(react_records or []) if react_records is not None else None
    dag_summary = summarize_records(dag_records or []) if dag_records is not None else None
    summary = {"react": react_summary, "tool_dag": dag_summary}
    (target / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (target / "report.md").write_text(
        build_markdown_report(react_summary, dag_summary),
        encoding="utf-8",
    )
    return summary
