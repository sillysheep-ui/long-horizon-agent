#!/usr/bin/env python3
"""RTX 6000D / Blackwell OPD 运行环境预检。"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import shutil
import sys
from typing import Any, Dict, List, Optional, Tuple


def version_tuple(value: str) -> Tuple[int, ...]:
    parts: List[int] = []
    for part in value.split("+")[0].split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def package_version(name: str) -> Optional[str]:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-gpus", type=int, default=2)
    parser.add_argument("--min-memory-gib", type=float, default=80.0)
    args = parser.parse_args()

    report: Dict[str, Any] = {"status": "FAIL", "blockers": [], "warnings": []}
    blockers: List[str] = report["blockers"]
    warnings: List[str] = report["warnings"]

    try:
        import torch
    except Exception as exc:  # pragma: no cover - 取决于服务器环境
        blockers.append(f"PyTorch 导入失败: {exc}")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    report["torch"] = torch.__version__
    report["torch_cuda"] = torch.version.cuda
    report["vllm"] = package_version("vllm")
    report["flash_attn"] = package_version("flash-attn")
    report["ms_swift"] = package_version("ms-swift")

    if version_tuple(torch.__version__) < (2, 7):
        blockers.append(f"PyTorch {torch.__version__} 太旧；Blackwell 需要 2.7+")
    if not torch.version.cuda or version_tuple(torch.version.cuda) < (12, 8):
        blockers.append(
            f"PyTorch CUDA 构建为 {torch.version.cuda!r}；Blackwell 需要 CUDA 12.8+ 构建"
        )
    if not torch.cuda.is_available():
        blockers.append("torch.cuda.is_available() 为 false")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    gpu_count = torch.cuda.device_count()
    report["gpu_count"] = gpu_count
    if gpu_count < args.min_gpus:
        blockers.append(f"至少需要 {args.min_gpus} 张 GPU，当前只有 {gpu_count} 张")

    gpus = []
    for index in range(gpu_count):
        props = torch.cuda.get_device_properties(index)
        memory_gib = props.total_memory / 1024**3
        capability = torch.cuda.get_device_capability(index)
        item = {
            "index": index,
            "name": props.name,
            "memory_gib": round(memory_gib, 2),
            "capability": f"{capability[0]}.{capability[1]}",
        }
        gpus.append(item)
        if memory_gib < args.min_memory_gib:
            blockers.append(
                f"GPU {index} 显存仅 {memory_gib:.1f} GiB，低于 {args.min_memory_gib:.1f} GiB"
            )
        if capability[0] < 10:
            warnings.append(
                f"GPU {index} 计算能力为 {capability[0]}.{capability[1]}，不是 Blackwell"
            )
    report["gpus"] = gpus

    try:
        device = torch.device("cuda:0")
        left = torch.randn((256, 256), device=device, dtype=torch.bfloat16)
        right = torch.randn((256, 256), device=device, dtype=torch.bfloat16)
        result = left @ right
        torch.cuda.synchronize(device)
        report["bf16_cuda_smoke"] = bool(torch.isfinite(result).all().item())
        if not report["bf16_cuda_smoke"]:
            blockers.append("Blackwell BF16 CUDA 计算结果非有限值")
    except Exception as exc:  # pragma: no cover - 取决于服务器环境
        report["bf16_cuda_smoke"] = False
        blockers.append(f"Blackwell BF16 CUDA 实测失败: {exc}")

    if report["vllm"] is None:
        blockers.append("未安装 vLLM")
    else:
        try:
            importlib.import_module("vllm")
            report["vllm_import"] = True
        except Exception as exc:  # pragma: no cover - 取决于服务器环境
            report["vllm_import"] = False
            blockers.append(f"vLLM 已安装但导入失败: {exc}")
    if shutil.which("swift") is None:
        blockers.append("找不到 swift 命令")
    if report["flash_attn"] is None:
        warnings.append("FlashAttention 不可用，6000D 启动脚本将回退到 SDPA")

    usable_students = max(0, min(3, gpu_count - 1))
    report["topology"] = {
        "teacher_gpu": 0 if gpu_count else None,
        "student_gpus": list(range(1, 1 + usable_students)),
        "student_processes": usable_students,
    }
    report["status"] = "PASS" if not blockers else "FAIL"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not blockers else 1


if __name__ == "__main__":
    sys.exit(main())
