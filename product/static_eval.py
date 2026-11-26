"""静态评测模式：锁定沙盒快照、禁止外部检索、生成可复现指纹。

静态评测的边界：
- 真值只能来自沙盒快照（坐标、营业时间、价格），不来自实时接口；
- 任何高德/网页检索都被拒绝，避免“跑的是离线数据、答案来自线上”；
- 模型 API 仍是唯一的网络依赖（生成与裁判），不在这层的管辖范围。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any


class StaticEvalError(RuntimeError):
    """静态评测模式下试图访问外部数据。"""


def enabled() -> bool:
    return os.getenv("STATIC_EVAL", "").strip().lower() in {"1", "true", "yes", "on"}


def configure() -> None:
    """打开静态模式：强制沙盒 POI，缺数据直接失败而不是静默降级。"""
    if not enabled():
        return
    os.environ["POI_PROVIDER"] = "sandbox"
    from tools import sandbox

    if not sandbox.available():
        raise StaticEvalError("静态评测需要 data/sandbox 快照，当前未找到")


def fingerprint(cases_path: Path) -> dict[str, Any]:
    """把“这次评测跑的是什么”固化成可对照的指纹。"""
    from product.constraints.spec import load_set
    from tools import sandbox

    payload = cases_path.read_bytes()
    return {
        "enabled": enabled(),
        "poi_provider": os.getenv("POI_PROVIDER", ""),
        "cases": str(cases_path),
        "cases_sha256": hashlib.sha256(payload).hexdigest()[:16],
        "constraint_set": load_set().version,
        "sandbox": sandbox.version(),
    }
