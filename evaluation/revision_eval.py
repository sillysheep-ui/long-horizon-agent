"""多轮修改评测：先规划一次，再提修改，检查“只改了该改的那天”。

用法：
    python3 -m evaluation.revision_eval --profile api_only
    python3 -m evaluation.revision_eval --ids rev-hangzhou-day2-quiet --limit 3

结果写到 results/product_eval/<时间戳>-revision-<profile>/。
"""

import argparse
import asyncio
import json
import os
import random
import time
from pathlib import Path
from typing import Any

from evaluation.contract import CheckRecord, SuiteReport, dataset_version
from evaluation.revision_checks import (
    check_answer_turn,
    check_followup,
    check_revision,
    summarize_revision,
)
from product.orchestrator import AgentWorkflow
from product.store import Store

CASES_PATH = Path(__file__).with_name("product_revision_cases.json")


def _titles(result: dict[str, Any], day_number: int) -> list[str]:
    for day in result.get("days") or []:
        if int(day.get("day") or 0) == day_number:
            return [str(activity.get("title") or "") for activity in day.get("activities") or []]
    return []


async def run_case(workflow: AgentWorkflow, store: Store, case: dict[str, Any]) -> dict[str, Any]:
    """一条用例 = 新建会话 → 首次规划 → 第二轮消息（改/问/补充）。"""
    record: dict[str, Any] = {"id": case["id"], "query": case["query"], "revision": case["revision"]}
    try:
        conversation = store.create_conversation()["conversation_id"]
        store.continue_conversation(conversation, case["query"])
        job = store.claim()
        if job is None:
            return {**record, "error": "首次任务未入队", "checks": {}}
        started = time.monotonic()
        before = await workflow.run(job)
        store.finish(job, before)
        record["first_elapsed"] = round(time.monotonic() - started, 2)
        record["first_status"] = before.get("status")
        version_before = store.latest_trip(conversation)["version"]

        second = store.continue_conversation(conversation, case["revision"])
        expect = case.get("expect", "targeted")
        if second.get("kind") == "answer":
            record["kind"] = "answer"
            record["reply"] = (second.get("reply") or {}).get("content", "")
            record["checks"] = check_answer_turn(case, record["reply"], version_before,
                                                 store.latest_trip(conversation)["version"],
                                                 before, store.latest_result(conversation) or before)
            return record

        job = store.claim()
        if job is None:
            return {**record, "error": "第二轮任务未入队", "checks": {}}
        started = time.monotonic()
        after = await workflow.run(job)
        store.finish(job, after)
        record["kind"] = second.get("kind")
        record["elapsed"] = round(time.monotonic() - started, 2)
        record["status"] = after.get("status")
        record["revision_applied"] = after.get("revision_applied")
        targets = [int(day) for day in case.get("target_days") or []]
        if expect == "followup":
            record["checks"] = check_followup(case, before, after)
        else:
            record["checks"] = check_revision(case, before, after)
        record["changed_days"] = [day for day in targets if _titles(after, day) != _titles(before, day)]
        record["before_days"] = [
            {"day": day.get("day"), "titles": _titles(before, int(day.get("day") or 0))}
            for day in before.get("days") or []
        ]
        record["after_days"] = [
            {"day": day.get("day"), "titles": _titles(after, int(day.get("day") or 0))}
            for day in after.get("days") or []
        ]
        record["notes"] = (after.get("notes") or [])[:3]
        return record
    except Exception as error:  # noqa: BLE001 - 评测需要把异常记进报告
        return {**record, "error": str(error)[:300], "checks": {}}


def write_report(target: Path, profile: str, summary: dict[str, Any], records: list[dict[str, Any]]) -> None:
    lines = [f"# 多轮修改评测 · {profile}", "",
             f"- 用例：{summary['cases']}",
             f"- 全部检查通过：{summary['all_passed']}/{summary['cases']}（{summary['all_passed_rate']:.0%}）",
             f"- 定向修改真正生效：{summary['targeted_with_change']}",
             f"- 首次规划均耗时：{summary['first_elapsed_mean']}s",
             f"- 修改响应均耗时：{summary['revision_elapsed_mean']}s", "",
             "## 检查通过率", "", "| 检查 | 通过率 |", "|---|---|"]
    lines += [f"| {name} | {rate:.0%} |" for name, rate in summary["pass_rate"].items()]
    lines += ["", "## 逐条结果", "",
              "| 用例 | 状态 | 定向生效 | 变化的日期 | 未通过检查 | 首次 | 修改 |",
              "|---|---|---|---|---|---|---|"]
    for record in records:
        failed = ",".join(name for name, ok in (record.get("checks") or {}).items() if ok is False) or "-"
        applied = record.get("revision_applied")
        label = record.get("status") or ("answer" if record.get("kind") == "answer" else record.get("error", "错误"))
        lines.append(f"| {record['id']} | {label} | "
                     f"{applied} | {record.get('changed_days') or '-'} | {failed} | "
                     f"{record.get('first_elapsed', '-')}s | {record.get('elapsed', '-')}s |")
    lines += ["", "## 修改前后对照", ""]
    for record in records:
        if not record.get("after_days"):
            continue
        lines.append(f"### {record['id']}")
        lines.append(f"修改：{record['revision']}")
        for before_day, after_day in zip(record.get("before_days") or [], record["after_days"], strict=False):
            lines.append(f"- 第 {after_day['day']} 天：{'/'.join(before_day['titles'])} → {'/'.join(after_day['titles'])}")
        lines.append("")
    (target / "report.md").write_text("\n".join(lines), encoding="utf-8")
    (target / "records.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="api_only")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--ids", default="", help="逗号分隔的用例 id")
    parser.add_argument("--db", default="")
    parser.add_argument("--out", default="")
    parser.add_argument("--seed", type=int, default=0, help="随机种子，写入报告便于复现")
    args = parser.parse_args()
    random.seed(args.seed)

    os.environ["MODEL_PROFILE"] = args.profile
    cases = json.loads(CASES_PATH.read_text())
    if args.ids:
        wanted = {item.strip() for item in args.ids.split(",") if item.strip()}
        cases = [case for case in cases if case["id"] in wanted]
    if args.limit:
        cases = cases[:args.limit]
    db_path = args.db or f"/tmp/revision-eval-{args.profile}.db"
    Path(db_path).unlink(missing_ok=True)

    store = Store(db_path)
    workflow = AgentWorkflow(store)
    records = []
    for index, case in enumerate(cases, 1):
        record = await run_case(workflow, store, case)
        records.append(record)
        failed = [name for name, ok in (record.get("checks") or {}).items() if ok is False]
        print(f"[{index}/{len(cases)}] {case['id']} status={record.get('status') or record.get('error')} "
              f"applied={record.get('revision_applied')} changed={record.get('changed_days')} failed={failed}")

    summary = summarize_revision(records)
    summary["seed"] = args.seed
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = Path(args.out or f"results/product_eval/{stamp}-revision-{args.profile}")
    target.mkdir(parents=True, exist_ok=True)
    write_report(target, args.profile, summary, records)
    per_case = [
        [CheckRecord(id=name, ok=value, layer="revision") for name, value in (record.get("checks") or {}).items()]
        for record in records
    ]
    report = SuiteReport(
        suite="revision",
        main_metric="all_passed_rate",
        main_value=summary["all_passed_rate"],
        cases=summary["cases"],
        dataset_version=dataset_version(CASES_PATH),
        evaluator_version="revision-checks-v1",
        checks=[item for group in per_case for item in group],
        metrics={
            "all_passed": summary["all_passed"],
            "pass_rate": summary["pass_rate"],
            "targeted_with_change": summary["targeted_with_change"],
            "first_elapsed_mean": summary["first_elapsed_mean"],
            "revision_elapsed_mean": summary["revision_elapsed_mean"],
        },
        labels={
            "comparable": False,
            "main_metric_note": "多轮检查全过的用例比例",
            "profile": args.profile,
            "seed": args.seed,
        },
    )
    report.write(target / "report.json")
    print(f"统一报告：suite=revision main=all_passed_rate {report.main_value} dataset={report.dataset_version}")
    print(json.dumps(summary, ensure_ascii=False))
    print(f"报告：{target / 'report.md'}")


if __name__ == "__main__":
    asyncio.run(main())
