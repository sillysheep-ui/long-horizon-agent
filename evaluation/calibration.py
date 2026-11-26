"""人工校准：把裁判结果导出成可人工评分的清单，并计算一致率。

用法：
    python3 -m evaluation.calibration export --records /tmp/calib50/records.json \
        --cases evaluation/static_cases.json --out /tmp/calib50/review
    # 人工填完 review.csv 的 human_score（1-5）后
    python3 -m evaluation.calibration compare --records /tmp/calib50/records.json \
        --labels /tmp/calib50/review/review.csv
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

JUDGE_OK = 0.7
HUMAN_OK = 4


def _itinerary(result: dict) -> str:
    lines: list[str] = []
    for day in result.get("days") or []:
        lines.append(f"第{day.get('day')}天 {day.get('city', '')}：{day.get('title', '')}")
        for item in day.get("activities") or []:
            lines.append(f"  {item.get('start', '')} {item.get('title', '')} "
                         f"（{item.get('duration', '')}）")
        for meal in day.get("meals") or []:
            lines.append(f"  餐 {meal.get('start', '')} {meal.get('title', '')} "
                         f"（{meal.get('duration', '')}）")
    return "\n".join(lines)


def _checks_summary(record: dict) -> str:
    failed = [name for name, ok in (record.get("checks") or {}).items() if ok is False]
    return "、".join(failed) if failed else "全过"


def export(records_path: Path, cases_path: Path, out_dir: Path) -> dict:
    records = json.loads(Path(records_path).read_text())
    cases = {case["id"]: case for case in json.loads(Path(cases_path).read_text())}
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    lines = ["# 人工校准清单", "", "评分：1 很差 / 2 勉强 / 3 一般 / 4 好 / 5 很好；"
             f"≥{HUMAN_OK} 视为可接受。", ""]
    for record in records:
        case = cases.get(record["id"], {})
        judge = record.get("judge") or {}
        itinerary = _itinerary(record.get("result") or {})
        rows.append({
            "id": record["id"],
            "city": "、".join(case.get("cities") or []),
            "days": case.get("days", ""),
            "difficulty": case.get("difficulty", ""),
            "query": record.get("query", ""),
            "hard_checks": _checks_summary(record),
            "judge_rubric": judge.get("rubric"),
            "judge_failed": "、".join(judge.get("failed") or []),
            "judge_fabrication": judge.get("fabrication"),
            "itinerary": itinerary.replace("\n", " / "),
            "human_score": "",
            "human_note": "",
        })
        lines += [f"## {record['id']}", "", f"问题：{record.get('query', '')}", "",
                  f"硬检查：{_checks_summary(record)}；裁判：{judge.get('rubric')}；"
                  f"未过 rubric：{'、'.join(judge.get('failed') or []) or '无'}", "",
                  "```", itinerary, "```", "", "人工评分：___ / 5    备注：", ""]
    with (out_dir / "review.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else ["id"])
        writer.writeheader()
        writer.writerows(rows)
    (out_dir / "review.md").write_text("\n".join(lines))
    return {"cases": len(rows), "csv": str(out_dir / "review.csv"),
            "markdown": str(out_dir / "review.md")}


def _kappa(pairs: list[tuple[bool, bool]]) -> float | None:
    if not pairs:
        return None
    total = len(pairs)
    observed = sum(1 for human, judge in pairs if human == judge) / total
    human_rate = sum(1 for human, _ in pairs if human) / total
    judge_rate = sum(1 for _, judge in pairs if judge) / total
    expected = human_rate * judge_rate + (1 - human_rate) * (1 - judge_rate)
    if expected >= 1:
        return 1.0 if observed == 1 else 0.0
    return (observed - expected) / (1 - expected)


def compare(records_path: Path, labels_path: Path) -> dict:
    records = json.loads(Path(records_path).read_text())
    labels = {row["id"]: row for row in csv.DictReader(Path(labels_path).open())}
    pairs: list[tuple[bool, bool]] = []
    gaps: list[float] = []
    for record in records:
        label = labels.get(record["id"]) or {}
        raw = (label.get("human_score") or "").strip()
        rubric = (record.get("judge") or {}).get("rubric")
        if not raw or rubric is None:
            continue
        human = float(raw)
        pairs.append((human >= HUMAN_OK, rubric >= JUDGE_OK))
        gaps.append(round(rubric - human / 5, 3))
    agreement = sum(1 for human, judge in pairs if human == judge) / len(pairs) if pairs else None
    return {"n": len(pairs), "agreement": round(agreement, 3) if agreement is not None else None,
            "kappa": round(_kappa(pairs), 3) if pairs else None,
            "judge_ok_rate": round(sum(1 for _, judge in pairs if judge) / len(pairs), 3) if pairs else None,
            "human_ok_rate": round(sum(1 for human, _ in pairs if human) / len(pairs), 3) if pairs else None,
            "mean_rubric": round(sum(r for r in (rec["judge"]["rubric"] for rec in records
                                                 if (rec.get("judge") or {}).get("rubric") is not None))
                                 / max(1, sum(1 for rec in records
                                              if (rec.get("judge") or {}).get("rubric") is not None)), 3),
            "mean_gap": round(sum(gaps) / len(gaps), 3) if gaps else None}


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    exp = sub.add_parser("export")
    exp.add_argument("--records", required=True)
    exp.add_argument("--cases", required=True)
    exp.add_argument("--out", required=True)
    cmp_ = sub.add_parser("compare")
    cmp_.add_argument("--records", required=True)
    cmp_.add_argument("--labels", required=True)
    args = parser.parse_args()
    if args.command == "export":
        print(json.dumps(export(Path(args.records), Path(args.cases), Path(args.out)),
                         ensure_ascii=False, indent=2))
    else:
        print(json.dumps(compare(Path(args.records), Path(args.labels)),
                         ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
