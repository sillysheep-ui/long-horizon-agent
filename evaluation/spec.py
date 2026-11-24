"""声明式评测配置：加载、校验、给 runner 提供默认值。

配置文件是 YAML；解析依赖 PyYAML（只在评测环境需要，不进入产品依赖）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

GRADER_TYPES = {"constraint_set", "llm_rubric", "state_check", "tool_calls", "transcript"}
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
        path = root / str(dataset["path"])
        if not path.exists():
            errors.append(f"数据集不存在：{dataset['path']}")
    split = dataset.get("split") if isinstance(dataset, dict) else None
    if split is not None and split not in SPLITS:
        errors.append(f"dataset.split 非法：{split}")

    graders = payload.get("graders") or []
    if not isinstance(graders, list) or not graders:
        errors.append("graders 必须是非空列表")
    kinds = set()
    for index, grader in enumerate(graders or []):
        kind = (grader or {}).get("type")
        kinds.add(kind)
        if kind not in GRADER_TYPES:
            errors.append(f"graders[{index}].type 未知：{kind}")
        if kind == "constraint_set":
            path = root / str(grader.get("path") or "")
            if not grader.get("path") or not path.exists():
                errors.append("constraint_set.path 不存在")
            threshold = grader.get("threshold", 1.0)
            if not 0 <= float(threshold) <= 1:
                errors.append("constraint_set.threshold 必须在 0–1")
        if kind == "llm_rubric":
            path = root / str(grader.get("rubric") or "")
            if not grader.get("rubric") or not path.exists():
                errors.append("llm_rubric.rubric 文件不存在")
            if not grader.get("assertions"):
                errors.append("llm_rubric.assertions 不能为空")
        if kind == "transcript":
            turns = grader.get("max_turns")
            if not isinstance(turns, int) or turns <= 0:
                errors.append("transcript.max_turns 必须是正整数")
        if kind == "tool_calls":
            for item in grader.get("required") or []:
                if not (item or {}).get("tool"):
                    errors.append("tool_calls.required 每项都要有 tool")

    thresholds = payload.get("thresholds") or {}
    for key in ("deliverable", "score_min"):
        if key in thresholds and not 0 <= float(thresholds[key]) <= 1:
            errors.append(f"thresholds.{key} 必须在 0–1")

    if "llm_rubric" in kinds and "constraint_set" not in kinds:
        errors.append("提示：有 rubric 但没有 constraint_set，硬约束不会被检查")

    return errors


def describe(payload: dict[str, Any]) -> dict[str, Any]:
    graders = payload.get("graders") or []
    return {
        "name": payload.get("name"),
        "version": payload.get("version"),
        "dataset": (payload.get("dataset") or {}).get("path"),
        "split": (payload.get("dataset") or {}).get("split"),
        "graders": [grader.get("type") for grader in graders],
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
