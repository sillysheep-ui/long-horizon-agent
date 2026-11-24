"""意图路由评测：规则覆盖多少、模型兜底多少、最终判对多少。

用法：
    python3 -m evaluation.intent_eval                      # 规则 + 模型兜底
    python3 -m evaluation.intent_eval --cases evaluation/intent_smoke.json  # 20 条冒烟
    python3 -m evaluation.intent_eval --no-model           # 只看规则
    python3 -m evaluation.intent_eval --ids int-chat-01,int-rev-03
"""

import argparse
import asyncio
import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

from evaluation.contract import CheckRecord, SuiteReport, dataset_version
from product.intent import INTENTS, classify_intent, route_intent
from product.model_gateway import ModelGateway

CASES_PATH = Path(__file__).with_name("intent_holdout.json")
RULES_ONLY = ("new_trip", "revise_trip", "ask")


def _accuracy(records: list[dict[str, Any]], key: str) -> float:
    if not records:
        return 0.0
    return round(sum(1 for record in records if record[key] == record["expect"]) / len(records), 3)


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    confusion: Counter[tuple[str, str]] = Counter()
    for record in records:
        if record["final"] != record["expect"]:
            confusion[(record["expect"], record["final"])] += 1
    per_class = {}
    for label in INTENTS:
        subset = [record for record in records if record["expect"] == label]
        per_class[label] = {
            "cases": len(subset),
            "rule": _accuracy(subset, "rule"),
            "final": _accuracy(subset, "final"),
        }
    return {
        "cases": len(records),
        "rule_accuracy": _accuracy(records, "rule"),
        "final_accuracy": _accuracy(records, "final"),
        "fallback_rate": round(sum(1 for r in records if r["source"] == "model") / max(1, len(records)), 3),
        "per_class": per_class,
        "confusion": [{"expect": expect, "got": got, "count": count}
                      for (expect, got), count in confusion.most_common()],
    }


async def run_case(model: ModelGateway, case: dict[str, Any], use_model: bool) -> dict[str, Any]:
    ruled = classify_intent(case["text"], case["has_trip"])
    record = {"id": case["id"], "text": case["text"], "expect": case["expect"],
              "rule": ruled["intent"], "final": ruled["intent"], "source": "rules", "confidence": None}
    if use_model and ruled["intent"] not in RULES_ONLY:
        final = await route_intent(model, case["text"], case["has_trip"])
        record.update({"final": final["intent"], "source": final.get("source", "model"),
                       "confidence": final.get("confidence")})
    return record


def write_report(target: Path, summary: dict[str, Any], records: list[dict[str, Any]], use_model: bool) -> None:
    lines = ["# 意图路由评测", "",
             f"- 模式：{'规则 + 模型兜底' if use_model else '仅规则'}",
             f"- 用例：{summary['cases']}",
             f"- 规则准确率：{summary['rule_accuracy']:.1%}",
             f"- 最终准确率：{summary['final_accuracy']:.1%}",
             f"- 模型兜底比例：{summary['fallback_rate']:.1%}", "",
             "| 意图 | 用例 | 规则 | 最终 |", "|---|---|---|---|"]
    for label, stats in summary["per_class"].items():
        lines.append(f"| {label} | {stats['cases']} | {stats['rule']:.0%} | {stats['final']:.0%} |")
    lines += ["", "## 判错", ""]
    failures = [record for record in records if record["final"] != record["expect"]]
    if not failures:
        lines.append("无")
    for record in failures:
        lines.append(f"- `{record['text']}`（期望 {record['expect']}，得到 {record['final']}，来源 {record['source']}）")
    target.mkdir(parents=True, exist_ok=True)
    (target / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (target / "records.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--ids", default="")
    parser.add_argument("--no-model", action="store_true")
    parser.add_argument("--cases", default="")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    cases_path = Path(args.cases) if args.cases else CASES_PATH
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    if args.ids:
        wanted = {item.strip() for item in args.ids.split(",") if item.strip()}
        cases = [case for case in cases if case["id"] in wanted]
    if args.limit:
        cases = cases[:args.limit]

    model = ModelGateway()
    started = time.monotonic()
    records = [await run_case(model, case, not args.no_model) for case in cases]
    summary = summarize(records)
    target = Path(args.out) if args.out else Path("results") / "intent_eval" / time.strftime("%Y%m%d-%H%M%S")
    write_report(target, summary, records, not args.no_model)
    report = SuiteReport(
        suite="intent",
        main_metric="final_accuracy",
        main_value=summary["final_accuracy"],
        cases=summary["cases"],
        dataset_version=dataset_version(cases_path),
        evaluator_version="intent-router-v1",
        checks=[CheckRecord(id=record["id"], ok=record["final"] == record["expect"], layer="intent")
                for record in records],
        metrics={
            "rule_accuracy": summary["rule_accuracy"],
            "final_accuracy": summary["final_accuracy"],
            "fallback_rate": summary["fallback_rate"],
            "per_class": summary["per_class"],
        },
        labels={"comparable": False, "main_metric_note": "意图分类准确率", "use_model": not args.no_model},
    )
    report.write(target / "report.json")
    print(f"统一报告：suite=intent main=final_accuracy {report.main_value} dataset={report.dataset_version}")
    print(json.dumps({"cases": summary["cases"], "rule_accuracy": summary["rule_accuracy"],
                      "final_accuracy": summary["final_accuracy"], "fallback_rate": summary["fallback_rate"],
                      "elapsed": round(time.monotonic() - started, 2)}, ensure_ascii=False))
    print("报告：" + str(target / "report.md"))


if __name__ == "__main__":
    asyncio.run(main())
