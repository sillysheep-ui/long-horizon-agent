"""声明式评测配置：加载、校验、给 runner 提供默认值。

配置文件是 YAML；解析依赖 PyYAML（只在评测环境需要，不进入产品依赖）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

GRADER_TYPES = {"constraint_set", "llm_rubric", "state_check", "tool_calls", "transcript"}
SEVERITIES = {"blocking", "scored", "info"}
GRADER_STATUS = {"implemented", "declared"}
METRIC_TYPES = {"latency", "tokens", "transcript"}
SPLITS = {"all", "dev", "test", "holdout"}


def _yaml():
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - 环境缺依赖时的提示
        raise RuntimeError("读取评测配置需要 PyYAML：pip install pyyaml") from exc
    return yaml


def load_spec(path: str | Path) -> dict[str, Any]:
    payload = _yaml().safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("评测配置必须是 YAML 对象")
    return payload


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _check_assertions(grader: dict[str, Any], where: str, errors: list[str]) -> None:
    assertions = grader.get("assertions")
    if assertions is None:
        return
    if not isinstance(assertions, list):
        errors.append(f"{where}.assertions 必须是列表")
        return
    seen: set[str] = set()
    for index, item in enumerate(assertions):
        if isinstance(item, str):
            continue
        if not isinstance(item, dict):
            errors.append(f"{where}.assertions[{index}] 必须是对象或字符串")
            continue
        if not item.get("text"):
            errors.append(f"{where}.assertions[{index}].text 不能为空")
        ident = item.get("id")
        if ident is not None:
            if ident in seen:
                errors.append(f"{where}.assertions id 重复：{ident}")
            seen.add(ident)


def _check_thresholds(thresholds: Any, errors: list[str]) -> None:
    if thresholds is None:
        return
    if not isinstance(thresholds, dict):
        errors.append("thresholds 必须是对象")
        return
    score_min = thresholds.get("score_min")
    if score_min is not None:
        value = _number(score_min)
        if value is None or not 0 <= value <= 1:
            errors.append("thresholds.score_min 必须在 0–1 或为 null")
    deliverable = thresholds.get("deliverable")
    if isinstance(deliverable, dict):
        rubric_min = deliverable.get("rubric_min")
        if rubric_min is not None and not 0 <= float(rubric_min) <= 1:
            errors.append("thresholds.deliverable.rubric_min 必须在 0–1")
    elif deliverable is not None:
        value = _number(deliverable)
        if value is None or not 0 <= value <= 1:
            errors.append("thresholds.deliverable 必须是 0–1 或对象")
    regression = thresholds.get("regression")
    if isinstance(regression, dict):
        drop = regression.get("max_drop")
        if drop is not None and (_number(drop) is None or float(drop) < 0):
            errors.append("thresholds.regression.max_drop 必须是非负数")
    for key, value in thresholds.items():
        if key in {"score_min", "deliverable", "regression"}:
            continue
        number = _number(value)
        if number is None:
            errors.append(f"thresholds.{key} 必须是数字")
        elif not 0 <= number <= 1:
            errors.append(f"thresholds.{key} 必须在 0–1")


def validate(payload: dict[str, Any], base: Path | None = None) -> list[str]:
    """返回错误列表；空列表表示通过。路径相对仓库根目录解析。"""
    root = base or Path.cwd()
    errors: list[str] = []
    for key in ("name", "version", "dataset", "graders"):
        if key not in payload:
            errors.append(f"缺少字段：{key}")

    dataset = payload.get("dataset") or {}
    if not isinstance(dataset, dict) or not dataset.get("path"):
        errors.append("dataset.path 必须存在")
    else:
        if not (root / str(dataset["path"])).exists():
            errors.append(f"数据集不存在：{dataset['path']}")
    split = dataset.get("split") if isinstance(dataset, dict) else None
    if split is not None and split not in SPLITS:
        errors.append(f"dataset.split 非法：{split}")

    judge = payload.get("judge")
    if judge is not None:
        if not isinstance(judge, dict):
            errors.append("judge 必须是对象")
        elif judge.get("enabled") is False:
            pass
        elif not judge.get("model"):
            errors.append("judge.model 必须存在")
        else:
            repeats = judge.get("repeats", 1)
            if not isinstance(repeats, int) or repeats < 1:
                errors.append("judge.repeats 必须是正整数")
            tokens = judge.get("max_tokens")
            if tokens is not None and (not isinstance(tokens, int) or tokens <= 0):
                errors.append("judge.max_tokens 必须是正整数")

    graders = payload.get("graders") or []
    if not isinstance(graders, list) or not graders:
        errors.append("graders 必须是非空列表")
    seen_ids: set[str] = set()
    kinds: set[str] = set()
    for index, grader in enumerate(graders or []):
        where = f"graders[{index}]"
        if not isinstance(grader, dict):
            errors.append(f"{where} 必须是对象")
            continue
        kind = grader.get("type")
        kinds.add(kind)
        if kind not in GRADER_TYPES:
            errors.append(f"{where}.type 未知：{kind}")
        ident = grader.get("id")
        if ident is not None:
            if ident in seen_ids:
                errors.append(f"{where}.id 重复：{ident}")
            seen_ids.add(ident)
        weight = grader.get("weight")
        if weight is not None and not 0 <= float(weight) <= 1:
            errors.append(f"{where}.weight 必须在 0–1")
        severity = grader.get("severity")
        if severity is not None and severity not in SEVERITIES:
            errors.append(f"{where}.severity 非法：{severity}")
        status = grader.get("status")
        if status not in GRADER_STATUS:
            errors.append(f"{where}.status 必须是 implemented 或 declared")
        _check_assertions(grader, where, errors)
        if kind == "constraint_set":
            path = root / str(grader.get("path") or "")
            if not grader.get("path") or not path.exists():
                errors.append(f"{where}.path 不存在")
            threshold = grader.get("threshold", 1.0)
            if not 0 <= float(threshold) <= 1:
                errors.append(f"{where}.threshold 必须在 0–1")
        if kind == "llm_rubric":
            rubric = grader.get("rubric")
            if not rubric or not (root / str(rubric)).exists():
                errors.append(f"{where}.rubric 文件不存在")
            if not grader.get("assertions"):
                errors.append(f"{where}.assertions 不能为空")
        if kind == "transcript":
            turns = grader.get("max_turns")
            if turns is not None and (not isinstance(turns, int) or turns <= 0):
                errors.append(f"{where}.max_turns 必须是正整数")
            if grader.get("status") == "implemented" and not isinstance(turns, int):
                errors.append(f"{where}.max_turns 在 implemented 时必填")
        if kind == "tool_calls":
            for item in grader.get("required") or []:
                if not (item or {}).get("tool"):
                    errors.append(f"{where}.required 每项都要有 tool")

    metrics = payload.get("tracked_metrics")
    if metrics is not None:
        if not isinstance(metrics, list):
            errors.append("tracked_metrics 必须是列表")
        for index, item in enumerate(metrics or []):
            kind = (item or {}).get("type")
            if kind not in METRIC_TYPES:
                errors.append(f"tracked_metrics[{index}].type 未知：{kind}")
            if not (item or {}).get("metrics"):
                errors.append(f"tracked_metrics[{index}].metrics 不能为空")

    _check_thresholds(payload.get("thresholds"), errors)

    if "llm_rubric" in kinds and "constraint_set" not in kinds:
        errors.append("提示：有 rubric 但没有 constraint_set，硬约束不会被检查")
    return errors


def describe(payload: dict[str, Any]) -> dict[str, Any]:
    graders = payload.get("graders") or []
    return {
        "name": payload.get("name"),
        "version": payload.get("version"),
        "suite": payload.get("suite"),
        "dataset": (payload.get("dataset") or {}).get("path"),
        "split": (payload.get("dataset") or {}).get("split"),
        "graders": [
            {
                "id": grader.get("id"),
                "type": grader.get("type"),
                "assertions": len(grader.get("assertions") or []),
                "severity": grader.get("severity"),
                "status": grader.get("status"),
            }
            for grader in graders
        ],
        "metrics": len(payload.get("tracked_metrics") or []),
        "score_min": (payload.get("thresholds") or {}).get("score_min"),
    }


def main() -> None:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="校验评测配置")
    parser.add_argument("spec", help="YAML 配置路径")
    args = parser.parse_args()
    payload = load_spec(args.spec)
    errors = validate(payload, Path.cwd())
    print(json.dumps(describe(payload), ensure_ascii=False))
    if errors:
        for error in errors:
            print(f"✗ {error}")
        raise SystemExit(1)
    print("✓ 配置校验通过")


if __name__ == "__main__":
    main()
