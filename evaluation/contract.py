"""评测报告的统一信封：四个 suite 用同一套结构装载与导出。

只统一“怎么装、怎么报”，不重新定义各 suite 的判定。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


@dataclass
class CheckRecord:
    """单条检查结果；ok=None 表示数据不足（unknown），不计入分母。"""

    id: str
    ok: bool | None
    layer: str = "single_turn"
    severity: str = "hard"
    detail: str = ""


@dataclass
class SuiteReport:
    """一个评测套件的完整报告。"""

    suite: str
    main_metric: str
    main_value: float | None
    cases: int
    dataset_version: str = ""
    evaluator_version: str = ""
    checks: list[CheckRecord] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    labels: dict[str, Any] = field(default_factory=dict)
    health: dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def write(self, target: Path) -> None:
        target.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n")


def dataset_version(path: Path | str) -> str:
    """用例文件的短哈希，用来标记“这批评测用的是哪版数据”。"""
    return sha256(Path(path).read_bytes()).hexdigest()[:8]
