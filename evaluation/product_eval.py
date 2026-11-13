"""产品行程生成质量评测：固定场景 + 确定性检查 + rubric 裁判。

用法：
    python3 -m evaluation.product_eval --profile api_only --limit 8
    python3 -m evaluation.product_eval --profile local_first --db /tmp/eval-local.db

结果写到 results/product_eval/<时间戳>-<profile>/，含 report.md 与 records.json。
"""

import argparse
import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List

from openai import AsyncOpenAI

from evaluation.product_checks import check_result, combined_score, summarize
from evaluation.product_judge import judge as judge_itinerary
from product.orchestrator import AgentWorkflow
from product.store import Store

CASES_PATH = Path(__file__).with_name("product_cases.json")


def _compact(result: Dict[str, Any]) -> Dict[str, Any]:
    days = []
    for day in result.get("days") or []:
        days.append({
            "day": day.get("day"),
            "city": day.get("city"),
            "activities": [{
                "start": a.get("start"), "duration": a.get("duration"),
                "title": a.get("title"), "description": a.get("description"),
                "transport": a.get("transport"), "cost": a.get("cost"),
                "evidence_id": a.get("evidence_id"),
            } for a in day.get("activities") or []],
        })
    evidence = result.get("evidence") or []
    return {
        "summary": result.get("summary"),
        "days": days,
        "notes": result.get("notes"),
        "evidence_stats": {
            "total": len(evidence),
            "ok": sum(1 for e in evidence if e.get("status") == "ok"),
            "simulated": sum(1 for e in evidence if e.get("simulated")),
            "sources": sorted({str(e.get("source")) for e in evidence}),
        },
    }


async def run_case(workflow: AgentWorkflow, store: Store, case: Dict[str, Any]) -> Dict[str, Any]:
    try:
        store.create_trip(case["query"])
        job = store.claim()
        if job is None:
            return {"id": case["id"], "error": "任务未入队", "checks": {}, "elapsed": 0.0}
        started = time.monotonic()
        result = await workflow.run(job)
        elapsed = round(time.monotonic() - started, 2)
        store.finish(job, result)
        return {"id": case["id"], "query": case["query"], "status": result.get("status"),
                "result": _compact(result), "checks": check_result(case, result), "elapsed": elapsed}
    except Exception as error:
        return {"id": case["id"], "query": case["query"], "error": str(error)[:300],
                "checks": {}, "elapsed": 0.0}


def write_report(target: Path, profile: str, summary: Dict[str, Any], records: List[Dict[str, Any]],
                 split: str = "all") -> None:
    lines = [f"# 产品行程评测 · {profile} · {split}", "", f"- 用例：{summary['cases']}",
             f"- 确定性检查全过：{summary['all_checks_passed']}/{summary['cases']}（平均通过率 {summary['hard_mean']}）",
             f"- rubric 均分：{summary['rubric_mean']}（0–1，10 条清单）",
             f"- 综合分均值：{summary['score_mean']}（0.6×硬检查 + 0.4×rubric − 编造扣分）",
             f"- 可交付：{summary['deliverable']}/{summary['cases']}",
             f"- 平均耗时：{summary['latency_mean']}s", "",
             "## 检查通过率", "", "| 检查 | 通过率 |", "|---|---|"]
    lines += [f"| {name} | {rate:.0%} |" for name, rate in summary["check_pass_rate"].items()]
    lines += ["", "## 逐条结果", "", "| 用例 | 状态 | 耗时 | 未通过检查 | rubric | 综合分 | 判定 |",
              "|---|---|---|---|---|---|---|"]
    for record in records:
        failed = ",".join(name for name, ok in record["checks"].items() if not ok) or "-"
        judge_info = record.get("judge") or {}
        rubric = judge_info.get("rubric")
        score = record.get("score")
        hard_ok = all(record["checks"].values()) if record["checks"] else False
        verdict = "可交付" if (hard_ok and rubric is not None and rubric >= 0.7 and not judge_info.get("fabrication")) \
            else ("待改进" if rubric is not None else "待人工")
        lines.append(f"| {record['id']} | {record.get('status') or record.get('error', '错误')[:20]} | "
                     f"{record['elapsed']}s | {failed} | {rubric} | {score} | {verdict} |")
    lines += ["", "## 裁判未通过的条目", ""]
    for record in records:
        judge_info = record.get("judge") or {}
        if judge_info.get("failed"):
            lines.append(f"- **{record['id']}**：未通过 {', '.join(judge_info['failed'])}")
        if judge_info.get("error"):
            lines.append(f"- **{record['id']}**：裁判失败 {judge_info['error']}")
    target.mkdir(parents=True, exist_ok=True)
    (target / "report.md").write_text("\n".join(lines) + "\n")
    (target / "records.json").write_text(json.dumps(records, ensure_ascii=False, indent=2))


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="api_only")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--db", default="")
    parser.add_argument("--out", default="")
    parser.add_argument("--skip-judge", action="store_true")
    parser.add_argument("--split", default="all", choices=["all", "dev", "test", "holdout"])
    parser.add_argument("--judge2-model", default="", help="第二裁判模型，用于估计裁判一致性")
    args = parser.parse_args()

    os.environ["MODEL_PROFILE"] = args.profile
    cases = [c for c in json.loads(CASES_PATH.read_text())
             if args.split == "all" or c.get("split") == args.split]
    if args.limit:
        cases = cases[:args.limit]
    db_path = args.db or f"/tmp/product-eval-{args.profile}.db"
    Path(db_path).unlink(missing_ok=True)

    store = Store(db_path)
    workflow = AgentWorkflow(store)
    client = AsyncOpenAI(base_url=os.environ.get("OPENAI_BASE_URL"), api_key=os.environ.get("OPENAI_API_KEY"),
                         timeout=120)
    judge_model = os.environ.get("JUDGE_MODEL_ID") or "deepseek-chat"

    records = []
    for index, case in enumerate(cases, 1):
        record = await run_case(workflow, store, case)
        if not args.skip_judge and record.get("result") and case.get("expect") != "needs_clarification":
            record["judge"] = await judge_itinerary(client, judge_model, case, record["result"])
            if args.judge2_model:
                second = await judge_itinerary(client, args.judge2_model, case, record["result"])
                first_rubric = record["judge"].get("rubric")
                second_rubric = second.get("rubric")
                record["judge"]["second_rubric"] = second_rubric
                record["judge"]["agreement"] = round(abs(first_rubric - second_rubric), 3) \
                    if first_rubric is not None and second_rubric is not None else None
            record["score"] = combined_score(record["checks"], (record.get("judge") or {}).get("rubric"),
                                             bool((record.get("judge") or {}).get("fabrication")))
        records.append(record)
        print(f"[{index}/{len(cases)}] {case['id']} status={record.get('status')} "
              f"elapsed={record['elapsed']}s failed={[k for k, v in record['checks'].items() if not v]} "
              f"rubric={(record.get('judge') or {}).get('rubric')} score={record.get('score')}")

    summary = summarize(records)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = Path(args.out or f"results/product_eval/{stamp}-{args.profile}")
    write_report(target, args.profile, summary, records, args.split)
    print(json.dumps(summary, ensure_ascii=False))
    print(f"报告：{target/'report.md'}")


if __name__ == "__main__":
    asyncio.run(main())
