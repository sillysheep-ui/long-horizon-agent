"""运行时约束集与评测约束集在共享检查上必须同义，避免“校验器报了但评测不算”。"""

import json
from pathlib import Path

SET_DIR = Path(__file__).resolve().parents[1] / "product" / "constraints" / "sets"


def _checks(name):
    return {item["id"]: item for item in json.loads((SET_DIR / name).read_text())["constraints"]}


def test_overlap_semantics_match_between_runtime_and_eval():
    runtime = _checks("runtime.json")["overlap"]["params"]
    evaluation = _checks("v1.json")["no_overlap"]["params"]
    assert runtime["include_meals"] == evaluation["include_meals"]
