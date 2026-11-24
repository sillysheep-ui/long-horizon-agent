"""在 ChinaTravel 的 query 上跑我们的流水线，给出一份「近似成绩单」。

只借他们的题目与约束标注，不实现他们的 DSL 评测器：
- 我们的分数 = 我们自己的 20 条约束检查；
- 他们的约束只做「可评估性」统计，不冒充 compliance rate。

用法：
    POI_PROVIDER=sandbox MODEL_PROFILE=api_only \\
    python3 -m evaluation.chinatravel --data-dir /tmp/ctq --limit 50 --out results/chinatravel/run1
"""

import argparse
import ast
import asyncio
import collections
import csv
import json
import random
import time
from pathlib import Path
from typing import Any

from evaluation.product_eval import run_case
from product.orchestrator import AgentWorkflow
from product.store import Store

TEN_CITIES = {"上海", "北京", "南京", "广州", "成都", "杭州", "武汉", "深圳", "苏州", "重庆"}

# 约束函数家族 → 我们能不能从自己的结果里判定
FAMILY_STATUS = {
    "day_count": ("covered", "天数"),
    "target_city": ("partial", "目的地城市"),
    "activity_type": ("partial", "活动类型（我们有 category 但未逐条映射）"),
    "activity_cost": ("partial", "活动费用（我们只有 cost 文本）"),
    "allactivities": ("partial", "活动集合（结构不同）"),
    "activity_transports": ("missing", "活动之间的交通方式/费用"),
    "innercity_transport_type": ("missing", "市内交通方式"),
    "innercity_transport_cost": ("missing", "市内交通费用"),
    "intercity_transport_type": ("missing", "城际交通方式"),
    "activity_tickets": ("missing", "景点门票"),
    "metro_tickets": ("missing", "地铁票"),
    "taxi_cars": ("missing", "出租车"),
    "room_count": ("missing", "房间数"),
    "room_type": ("missing", "房型"),
    "accommodation_type": ("missing", "住宿类型"),
    "restaurant_type": ("partial", "餐饮类型"),
    "attraction_type": ("partial", "景点类型"),
    "activity_position": ("partial", "活动位置"),
    "people_count": ("missing", "人数"),
}


def load_rows(data_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name in ("easy", "medium"):
        path = data_dir / f"{name}.csv"
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if row.get("target_city") not in TEN_CITIES:
                    continue
                try:
                    constraints = ast.literal_eval(row.get("hard_logic_py") or "[]")
                except (ValueError, SyntaxError):
                    constraints = []
                rows.append({
                    "id": f"ct-{row['uid']}",
                    "query": row["nature_language"],
                    "days": int(row.get("days") or 1),
                    "cities": [row["target_city"]],
                    "expect": "",
                    "source": name,
                    "constraints": constraints,
                })
    return rows


def constraint_families(rows: list[dict[str, Any]]) -> collections.Counter:
    counter: collections.Counter = collections.Counter()
    for row in rows:
        for expression in row["constraints"]:
            try:
                tree = ast.parse(str(expression))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    counter[node.func.id] += 1
    return counter


def coverage(families: collections.Counter) -> dict[str, Any]:
    total = sum(families.values())
    covered = sum(count for name, count in families.items() if FAMILY_STATUS.get(name, ("missing",))[0] == "covered")
    partial = sum(count for name, count in families.items() if FAMILY_STATUS.get(name, ("missing",))[0] == "partial")
    table = []
    for name, count in families.most_common():
        status, label = FAMILY_STATUS.get(name, ("missing", "未知"))
        table.append({"function": name, "count": count, "status": status, "label": label})
    return {"total": total, "covered": covered, "partial": partial,
            "covered_rate": round((covered + partial * 0.5) / max(1, total), 3), "table": table}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="/tmp/ctq")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--profile", default="api_only")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    rows = load_rows(Path(args.data_dir))
    if not rows:
        raise SystemExit(f"{args.data_dir} 下没有 easy.csv/medium.csv")
    random.seed(args.seed)
    easy = [row for row in rows if row["source"] == "easy"]
    medium = [row for row in rows if row["source"] == "medium"]
    random.shuffle(easy)
    random.shuffle(medium)
    easy_target = min(len(easy), int(args.limit * 0.6))
    sample = easy[:easy_target] + medium[: max(0, args.limit - easy_target)]

    out_dir = Path(args.out or f"results/chinatravel/{time.strftime('%Y%m%d-%H%M%S')}")
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = f"/tmp/chinatravel-{args.profile}.db"
    store = Store(db_path)
    workflow = AgentWorkflow(store)
    records = []
    started_at = time.monotonic()
    for index, case in enumerate(sample, 1):
        record = await run_case(workflow, store, case)
        record["constraints"] = case["constraints"]
        record["source"] = case["source"]
        failed = [key for key, value in (record.get("checks") or {}).items() if value is False]
        record["failed_checks"] = failed
        records.append(record)
        print(f"[{index}/{len(sample)}] {case['id']} {case['source']} status={record.get('status')} "
              f"elapsed={record.get('elapsed')}s failed={failed}", flush=True)

    all_passed = sum(1 for record in records if record.get("checks") and all(
        value is not False for value in record["checks"].values()))
    families = constraint_families(sample)
    cov = coverage(families)
    summary = {
        "cases": len(records),
        "all_checks_passed": all_passed,
        "macro": round(all_passed / max(1, len(records)), 3),
        "elapsed_mean": round(sum(record.get("elapsed") or 0 for record in records) / max(1, len(records)), 2),
        "elapsed_total": round(time.monotonic() - started_at, 1),
        "constraint_coverage": cov,
        "data_source": "LAMDA-NeSy/ChinaTravel (CC-BY-4.0, easy+medium)",
        "profile": args.profile,
    }
    (out_dir / "records.json").write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print("报告目录：", out_dir)


if __name__ == "__main__":
    asyncio.run(main())
