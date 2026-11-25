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
import random
import time
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from evaluation.contract import CheckRecord, SuiteReport, dataset_version
from evaluation.metrics import pass_metrics
from evaluation.metrics import summarize as summarize_metrics
from evaluation.product_checks import CONSTRAINT_SET, check_result, combined_score, combined_score_v2, summarize
from evaluation.product_judge import judge as judge_itinerary
from evaluation.stage_checks import evaluator_health, summarize_trajectory
from product.orchestrator import AgentWorkflow
from product.store import Store
from tools import sandbox as sandbox_data

CASES_PATH = Path(__file__).with_name("product_cases.json")


def _compact(result: dict[str, Any]) -> dict[str, Any]:
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
            "meals": [{
                "start": m.get("start"), "duration": m.get("duration"),
                "title": m.get("title"), "evidence_id": m.get("evidence_id"),
            } for m in day.get("meals") or [] if isinstance(m, dict)],
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


async def run_case(workflow: AgentWorkflow, store: Store, case: dict[str, Any]) -> dict[str, Any]:
    try:
        store.create_trip(case["query"])
        job = store.claim()
        if job is None:
            return {"id": case["id"], "error": "任务未入队", "checks": {}, "elapsed": 0.0}
        started = time.monotonic()
        result = await workflow.run(job)
        elapsed = round(time.monotonic() - started, 2)
        store.finish(job, result)
        trajectory = summarize_trajectory(case, result)
        trace = result.get("trace") or {}
        trajectory["latency_ms"] = {child.get("name"): child.get("latency_ms") or 0
                                    for child in trace.get("children") or []}
        return {"id": case["id"], "query": case["query"], "status": result.get("status"),
                "pool": case.get("pool", "unknown"), "result": _compact(result),
                "checks": check_result(case, result), "trajectory": trajectory, "elapsed": elapsed}
    except Exception as error:
        return {"id": case["id"], "query": case["query"], "pool": case.get("pool", "unknown"),
                "error": str(error)[:300], "checks": {}, "elapsed": 0.0}


def _stage_line(stages: dict[str, float] | None) -> str:
    if not stages:
        return "未记录"
    return " / ".join(f"{name} {value}s" for name, value in stages.items())


def write_report(target: Path, profile: str, summary: dict[str, Any], records: list[dict[str, Any]],
                 split: str = "all", health: dict[str, Any] | None = None) -> None:
    lines = [f"# 产品行程评测 · {profile} · {split}", "", f"- 用例：{summary['cases']}",
             f"- 确定性检查全过：{summary['all_checks_passed']}/{summary['cases']}（平均通过率 {summary['hard_mean']}）",
             f"- 检查 micro/macro：{summary.get('checks_micro')}/{summary.get('checks_macro')}"
             f"（约束集 v{summary.get('constraint_set')}）",
             f"- rubric 均分：{summary['rubric_mean']}（0–1，10 条清单）",
             f"- 综合分均值：{summary['score_mean']}（0.6×硬检查 + 0.4×rubric − 编造扣分）",
             f"- 信号分均值：{summary['score_signal_mean']}（门槛检查不计分，失败直接封顶 0.2）",
             f"- 可交付：{summary['deliverable']}/{summary['cases']}",
             f"- 平均耗时：{summary['latency_mean']}s",
             f"- 分段耗时：{_stage_line(summary.get('stage_latency_mean'))}",
             f"- 随机种子：{summary.get('seed')} / 裁判模型：{summary.get('judge_model')}",
             f"- 分池：沙盒 {summary.get('pools', {}).get('sandbox', 0)} 条（全过 {summary.get('pool_pass', {}).get('sandbox')}）"
             f" / 联网 {summary.get('pools', {}).get('live', 0)} 条（全过 {summary.get('pool_pass', {}).get('live')}）",
             f"- 沙盒数据：{summary.get('sandbox_data', {}).get('tables') or '未加载'}", "",
             "## 检查通过率", "", "| 检查 | 通过率 |", "|---|---|"]
    if summary.get("pass_metrics"):
        metrics = summary["pass_metrics"]
        count = metrics["trials"]
        lines.insert(-1, f"- 多次采样：{count} trials，pass@1 {metrics['pass_at_1']} / "
                         f"pass@{count} {metrics['pass_at_k']} / pass^{count} {metrics['pass_hat_k']}"
                         f"（逐 trial {metrics['per_trial_pass']}）")
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
    stage_failed: dict[str, list[str]] = {}
    stage_totals: dict[str, list[bool]] = {}
    error_counts: dict[tuple, int] = {}
    attributions: dict[str, int] = {}
    branch_rates: dict[str, list[float]] = {}
    for record in records:
        trajectory = record.get("trajectory") or {}
        for stage, info in (trajectory.get("stages") or {}).items():
            for check in info.get("checks") or []:
                if check["passed"] is None:
                    continue
                stage_totals.setdefault(stage, []).append(bool(check["passed"]))
                if not check["passed"]:
                    stage_failed.setdefault(stage, []).append(check["name"])
        for error in trajectory.get("errors") or []:
            key = (error["stage"], error["type"])
            error_counts[key] = error_counts.get(key, 0) + 1
        primary = (trajectory.get("attribution") or {}).get("primary")
        if primary and primary != "none":
            attributions[primary] = attributions.get(primary, 0) + 1
        for branch, info in (trajectory.get("branches") or {}).items():
            if info.get("rate") is not None:
                branch_rates.setdefault(branch, []).append(info["rate"])

    lines += ["", "## 分阶段", "", "| 阶段 | 分数 | 主要未通过项 |", "|---|---|---|"]
    for stage in ("intake", "planning", "retrieval", "coverage", "answer"):
        values = stage_totals.get(stage) or []
        score = round(sum(1 for value in values if value) / len(values), 3) if values else "unknown"
        failed = sorted(set(stage_failed.get(stage) or []))[:4]
        lines.append(f"| {stage} | {score} | {', '.join(failed) or '-'} |")

    lines += ["", "## 分支覆盖", "", "| 分支 | 平均覆盖率 |", "|---|---|"]
    for branch in ("poi", "weather", "hotel", "transport"):
        values = branch_rates.get(branch)
        lines.append(f"| {branch} | {round(sum(values) / len(values), 3) if values else 'skipped'} |")

    lines += ["", "## 归因", ""]
    if attributions:
        for source, count in sorted(attributions.items(), key=lambda item: -item[1]):
            lines.append(f"- {source}: {count}")
    else:
        lines.append("- 无失败样本")

    lines += ["", "## 错误分布", "", "| 阶段 | 类型 | 次数 |", "|---|---|---|"]
    for (stage, error_type), count in sorted(error_counts.items(), key=lambda item: -item[1])[:10]:
        lines.append(f"| {stage} | {error_type} | {count} |")

    health = health or evaluator_health(records)
    lines += ["", "## 裁判健康度", "",
              f"- 判定样本：{health['judged']}",
              f"- 截断：{health['truncated']}，解析失败：{health['parse_failed']}",
              f"- 双裁判平均分差：{health['mean_agreement']}",
              f"- 结论：{'可信' if health['reliable'] else '不可用（裁判问题，先修裁判）'}"]
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
    parser.add_argument("--judge-model", default="", help="覆盖 JUDGE_MODEL_ID")
    parser.add_argument("--seed", type=int, default=0, help="随机种子，写入报告便于复现")
    parser.add_argument("--min-score", type=float, default=None, help="综合分低于该值时退出码 1")
    parser.add_argument("--cases", default="", help="用例文件，默认 evaluation/product_cases.json")
    parser.add_argument("--ids", default="", help="只跑指定用例，逗号分隔")
    parser.add_argument("--pool", default="", choices=["", "sandbox", "live", "unknown"],
                        help="只跑某个数据池：sandbox/live/unknown")
    parser.add_argument("--spec", default="", help="声明式评测配置（YAML），可用 --cases/--split 覆盖")
    parser.add_argument("--trials", type=int, default=1, help="同一批用例重复采样次数，>1 时报告 pass@k / pass^k")
    parser.add_argument("--min-pass-rate", type=float, default=None, help="确定性检查全过比例低于该值时退出码 1")
    args = parser.parse_args()
    random.seed(args.seed)

    os.environ["MODEL_PROFILE"] = args.profile
    spec: dict = {}
    if args.spec:
        from evaluation.spec import load_spec, validate

        spec = load_spec(args.spec)
        problems = validate(spec)
        if problems:
            raise SystemExit("评测配置有误：" + "；".join(problems))
    spec_dataset = (spec.get("dataset") or {}) if spec else {}
    cases_path = Path(args.cases or spec_dataset.get("path") or "") if (args.cases or spec_dataset) else CASES_PATH
    split = args.split
    if split == "all" and spec_dataset.get("split"):
        split = str(spec_dataset["split"])
    cases = [c for c in json.loads(cases_path.read_text())
             if split == "all" or c.get("split") == split]
    if args.ids:
        wanted = {item.strip() for item in args.ids.split(",") if item.strip()}
        cases = [c for c in cases if c.get("id") in wanted]
    if args.limit:
        cases = cases[:args.limit]
    sandbox_cities = sandbox_data.cities()
    for case in cases:
        cities = {sandbox_data.normalize_city(item) for item in case.get("cities") or []}
        if not cities or not sandbox_cities:
            case["pool"] = "unknown"
        else:
            case["pool"] = "sandbox" if cities <= sandbox_cities else "live"

    if args.pool:
        cases = [case for case in cases if case.get("pool") == args.pool]
        if not cases:
            raise SystemExit(f"没有 pool={args.pool} 的用例")
    db_path = args.db or f"/tmp/product-eval-{args.profile}.db"
    trials = max(1, int(args.trials))
    client = AsyncOpenAI(base_url=os.environ.get("OPENAI_BASE_URL"), api_key=os.environ.get("OPENAI_API_KEY"),
                         timeout=120)
    judge_model = args.judge_model or os.environ.get("JUDGE_MODEL_ID") or "deepseek-chat"

    records: list[dict[str, Any]] = []
    per_case_trials: dict[str, list[bool]] = {case["id"]: [] for case in cases}
    for trial in range(trials):
        trial_db = db_path if trial == 0 else db_path.replace(".db", f"-t{trial}.db")
        Path(trial_db).unlink(missing_ok=True)
        if trial:
            random.seed(args.seed + trial)
        store = Store(trial_db)
        workflow = AgentWorkflow(store)
        for index, case in enumerate(cases, 1):
            record = await run_case(workflow, store, case)
            per_case_trials[case["id"]].append(bool(record["checks"]) and all(record["checks"].values()))
            if trial == 0:
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
                    record["score_signal"] = combined_score_v2(
                        record["checks"], (record.get("judge") or {}).get("rubric"),
                        bool((record.get("judge") or {}).get("fabrication")))
                records.append(record)
            print(f"[trial {trial + 1}/{trials}] [{index}/{len(cases)}] {case['id']} status={record.get('status')} "
                  f"elapsed={record['elapsed']}s failed={[k for k, v in record['checks'].items() if not v]} "
                  f"rubric={(record.get('judge') or {}).get('rubric')} score={record.get('score')}")

    summary = summarize(records)
    summary["seed"] = args.seed
    summary["judge_model"] = judge_model
    summary["constraint_set"] = CONSTRAINT_SET.version
    summary["pools"] = {pool: sum(1 for record in records if record.get("pool") == pool)
                        for pool in ("sandbox", "live", "unknown")}
    summary["pool_pass"] = {
        pool: round(sum(1 for record in records
                        if record.get("pool") == pool and all(record["checks"].values()))
                    / max(1, summary["pools"][pool]), 3)
        for pool in ("sandbox", "live")}
    if trials > 1:
        summary["trials"] = trials
        summary["pass_metrics"] = pass_metrics(list(per_case_trials.values()))
    summary["sandbox_data"] = {
        "cities": sorted(sandbox_cities),
        "tables": {name: len(sandbox_data._table(name)) for name in sandbox_data._TABLES},
        "version": sandbox_data.version(),
    } if sandbox_cities else {}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = Path(args.out or f"results/product_eval/{stamp}-{args.profile}")
    health = evaluator_health(records)
    summary["evaluator"] = health
    write_report(target, args.profile, summary, records, args.split, health)
    pools = {record.get("pool") for record in records}
    layer = "sandbox" if pools == {"sandbox"} else "single_turn"
    per_case = [
        [CheckRecord(id=name, ok=value, layer=layer) for name, value in record["checks"].items()]
        for record in records
    ]
    scores = [float(record["score"]) for record in records if record.get("score") is not None]
    judge_ran = summary["score_mean"] is not None
    report = SuiteReport(
        suite=layer,
        main_metric="score_mean" if judge_ran else "checks_macro",
        main_value=summary["score_mean"] if judge_ran else summary["checks_macro"],
        cases=len(records),
        dataset_version=dataset_version(cases_path),
        evaluator_version=f"constraints-{CONSTRAINT_SET.version}",
        checks=[item for group in per_case for item in group],
        metrics={
            **summarize_metrics(per_case, scores),
            "score_mean": summary["score_mean"],
            "score_signal_mean": summary["score_signal_mean"],
            "signal_hard_mean": summary["signal_hard_mean"],
            "gates_passed": summary["gates_passed"],
            "rubric_mean": summary["rubric_mean"],
            "deliverable": summary["deliverable"],
            "latency_mean": summary["latency_mean"],
            "pass_metrics": summary.get("pass_metrics"),
        },
        labels={
            "comparable": False,
            "main_metric_note": "0.6×硬检查 + 0.4×rubric − 0.2×编造",
            "split": split,
            "profile": args.profile,
            "pool": layer,
        },
        health=health,
    )
    report.write(target / "report.json")
    print(f"统一报告：suite={report.suite} main={report.main_metric} {report.main_value} "
          f"dataset={report.dataset_version} checks={len(report.checks)}")
    print(json.dumps(summary, ensure_ascii=False))
    print(f"报告：{target/'report.md'}")
    if summary.get("pass_metrics"):
        metrics = summary["pass_metrics"]
        print(f"多次采样 {metrics['trials']} trials：pass@1 {metrics['pass_at_1']} / "
              f"pass@{metrics['trials']} {metrics['pass_at_k']} / pass^{metrics['trials']} {metrics['pass_hat_k']}"
              f"（逐 trial {metrics['per_trial_pass']}）")
    min_pass_rate = args.min_pass_rate
    if min_pass_rate is None and spec:
        min_pass_rate = (spec.get("thresholds") or {}).get("pass_rate_min")
    if min_pass_rate is not None:
        if summary.get("pass_metrics"):
            rate = summary["pass_metrics"]["pass_hat_k"]
        else:
            rate = summary["all_checks_passed"] / max(1, summary["cases"])
        if rate < float(min_pass_rate):
            print(f"确定性检查全过 {rate:.3f} 低于阈值 {min_pass_rate}")
            raise SystemExit(1)
    min_score = args.min_score
    if min_score is None and spec:
        min_score = (spec.get("thresholds") or {}).get("score_min")
    if min_score is not None and summary["score_mean"] is not None and float(summary["score_mean"]) < float(min_score):
        print(f"综合分 {summary['score_mean']} 低于阈值 {min_score}")
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
