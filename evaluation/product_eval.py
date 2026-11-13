"""产品行程生成质量评测：固定场景 + 确定性检查 + 裁判打分。

用法：
    python3 -m evaluation.product_eval --profile api_only --limit 8
    python3 -m evaluation.product_eval --profile local_first --db /tmp/eval-local.db

结果写到 results/product_eval/<时间戳>-<profile>/，含 report.md 与 records.json。
"""

import argparse
import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from openai import AsyncOpenAI

from evaluation.product_checks import check_result, summarize
from product.model_gateway import parse_json
from product.orchestrator import AgentWorkflow
from product.store import Store

CASES_PATH = Path(__file__).with_name("product_cases.json")

JUDGE_SYSTEM_PROMPT = """你是旅行规划评审。只根据给定行程文本和证据统计打分，不要补充外部知识。
评分维度（各 1-5 分，5 最好）：
1. concreteness 具体性：是否写清每天几点、停留多久、具体看什么，而不是空泛描述。
2. feasibility 可执行性：路线顺序、交通衔接、单日强度是否合理。
3. preference_fit 偏好匹配：是否回应了用户的偏好、预算、同行人。
4. fact_boundary 事实边界：是否把没核实的价格/营业时间/班次标为未核实，是否编造。
5. richness 丰富度：两天以上的行程是否有足够变化，不重复、不空洞。
只输出 JSON：{"scores":{"concreteness":4,"feasibility":4,"preference_fit":3,"fact_boundary":5,"richness":3},"overall":3.8,"comments":["一句话点评","最需要改的一点"]}。overall 取五项均值。"""


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


async def judge(client: AsyncOpenAI, model: str, case: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    payload = json.dumps({"query": case["query"], "itinerary": _compact(result)}, ensure_ascii=False)
    try:
        for attempt in range(2):
            response = await client.chat.completions.create(
                model=model, temperature=0.0,
                messages=[{"role": "system", "content": JUDGE_SYSTEM_PROMPT},
                          {"role": "user", "content": payload + "\n\n只输出 JSON，不要解释。"
                                                        + ("直接给分数，不要分析。" if attempt else "")}],
                max_tokens=4000,
            )
            content = response.choices[0].message.content or ""
            if response.choices[0].finish_reason != "length":
                break
        if response.choices[0].finish_reason == "length":
            return {"error": "裁判输出被截断", "overall": None, "raw": content[:200]}
        parsed = parse_json(content)
        scores = parsed.get("scores") or {}
        overall = parsed.get("overall")
        if overall is None and scores:
            overall = round(sum(float(v) for v in scores.values()) / len(scores), 2)
        return {"scores": scores, "overall": overall, "comments": parsed.get("comments") or []}
    except Exception as error:  # 裁判失败不能拖垮整轮评测
        return {"error": str(error)[:300], "overall": None}


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


def write_report(target: Path, profile: str, summary: Dict[str, Any], records: List[Dict[str, Any]]) -> None:
    lines = [f"# 产品行程评测 · {profile}", "", f"- 用例：{summary['cases']}",
             f"- 全部检查通过：{summary['all_checks_passed']}/{summary['cases']}",
             f"- 裁判均分：{summary['judge_mean']}", f"- 平均耗时：{summary['latency_mean']}s", "",
             "## 检查通过率", "", "| 检查 | 通过率 |", "|---|---|"]
    lines += [f"| {name} | {rate:.0%} |" for name, rate in summary["check_pass_rate"].items()]
    lines += ["", "## 逐条结果", "", "| 用例 | 状态 | 耗时 | 未通过检查 | 裁判分 |", "|---|---|---|---|---|"]
    for record in records:
        failed = ",".join(name for name, ok in record["checks"].items() if not ok) or "-"
        judge_score = (record.get("judge") or {}).get("overall")
        lines.append(f"| {record['id']} | {record.get('status') or record.get('error', '错误')[:20]} | "
                     f"{record['elapsed']}s | {failed} | {judge_score} |")
    lines += ["", "## 裁判点评", ""]
    for record in records:
        judge_info = record.get("judge") or {}
        if judge_info.get("comments"):
            lines.append(f"- **{record['id']}**：" + "；".join(judge_info["comments"][:2]))
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
    args = parser.parse_args()

    os.environ["MODEL_PROFILE"] = args.profile
    cases = json.loads(CASES_PATH.read_text())
    if args.limit:
        cases = cases[:args.limit]
    db_path = args.db or f"/tmp/product-eval-{args.profile}.db"
    for stale in (db_path,):
        Path(stale).unlink(missing_ok=True)

    store = Store(db_path)
    workflow = AgentWorkflow(store)
    client = AsyncOpenAI(base_url=os.environ.get("OPENAI_BASE_URL"), api_key=os.environ.get("OPENAI_API_KEY"),
                         timeout=120)
    judge_model = os.environ.get("JUDGE_MODEL_ID") or "deepseek-chat"

    records = []
    for index, case in enumerate(cases, 1):
        record = await run_case(workflow, store, case)
        if not args.skip_judge and record.get("result") and case.get("expect") != "needs_clarification":
            record["judge"] = await judge(client, judge_model, case, record["result"])
        records.append(record)
        print(f"[{index}/{len(cases)}] {case['id']} status={record.get('status')} "
              f"elapsed={record['elapsed']}s failed={[k for k, v in record['checks'].items() if not v]}")

    summary = summarize(records)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = Path(args.out or f"results/product_eval/{stamp}-{args.profile}")
    write_report(target, args.profile, summary, records)
    print(json.dumps(summary, ensure_ascii=False))
    print(f"报告：{target/'report.md'}")


if __name__ == "__main__":
    asyncio.run(main())
