#!/usr/bin/env python3
"""OPD stage1 静态预检；存在阻断项时返回 2。"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]


def model_config(path: Path) -> dict:
    config = path / "config.json"
    return json.loads(config.read_text(encoding="utf-8")) if config.exists() else {}


def sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--train", default="data/final/opd_stage1/train.jsonl")
    parser.add_argument("--dev", default="data/final/opd_stage1/dev.jsonl")
    parser.add_argument(
        "--teacher",
        default="saved/output/teacher_grpo_formal20_8gpu80gb_checkpoint20_merged_20260820",
    )
    parser.add_argument(
        "--student",
        default="saved/output/grpo_parser_aligned_run/v8-20260514-140934/checkpoint-150",
    )
    parser.add_argument("--output", default="saved/output/opd_stage1_preflight.json")
    args = parser.parse_args()

    blockers: list[str] = []
    warnings: list[str] = []
    paths = {name: Path(value) for name, value in vars(args).items() if name != "output"}
    for name, path in paths.items():
        if not path.exists():
            blockers.append(f"缺少 {name}: {path}")

    train = load_jsonl(paths["train"]) if paths["train"].exists() else []
    dev = load_jsonl(paths["dev"]) if paths["dev"].exists() else []
    train_ids = {str(row.get("trajectory_id")) for row in train}
    dev_ids = {str(row.get("trajectory_id")) for row in dev}
    if train_ids & dev_ids:
        blockers.append("训练集与验证集存在 trajectory_id 泄漏")

    decision_counts = Counter()
    literal_placeholders = 0
    for split, rows in (("train", train), ("dev", dev)):
        for index, row in enumerate(rows):
            messages = row.get("messages", [])
            if not messages or messages[-1].get("role") != "assistant":
                blockers.append(f"{split}[{index}] 最后一条消息不是 assistant")
                continue
            if len(messages) < 3:
                blockers.append(f"{split}[{index}] 缺少有效上下文")
            decision_counts[f"{split}:{row.get('decision_type', 'missing')}"] += 1
            system_text = "\n".join(
                str(item.get("content", "")) for item in messages if item.get("role") == "system"
            )
            literal_placeholders += system_text.count("<answer>...</answer>")
    if literal_placeholders:
        blockers.append(f"system prompt 仍含 {literal_placeholders} 个字面答案占位符")
    if not train or not dev:
        blockers.append("OPD train/dev 至少有一方为空")

    teacher_cfg = model_config(paths["teacher"])
    student_cfg = model_config(paths["student"])
    if paths["teacher"].exists() and not teacher_cfg:
        blockers.append("教师目录缺少 config.json")
    if paths["student"].exists() and not student_cfg:
        blockers.append("学生模型目录缺少 config.json")
    if teacher_cfg and student_cfg:
        if teacher_cfg.get("model_type") != student_cfg.get("model_type"):
            blockers.append("教师与学生 model_type 不一致")
        if teacher_cfg.get("vocab_size") != student_cfg.get("vocab_size"):
            blockers.append("教师与学生 vocab_size 不一致，不能直接逐 token 蒸馏")
    else:
        warnings.append("学生模型尚未就绪，暂时无法完成 tokenizer/vocab 对齐检查")

    tokenizer_hashes = {
        "teacher_tokenizer_json": sha256(paths["teacher"] / "tokenizer.json"),
        "student_tokenizer_json": sha256(paths["student"] / "tokenizer.json"),
        "teacher_vocab_json": sha256(paths["teacher"] / "vocab.json"),
        "student_vocab_json": sha256(paths["student"] / "vocab.json"),
    }
    for artifact in ("tokenizer_json", "vocab_json"):
        teacher_hash = tokenizer_hashes[f"teacher_{artifact}"]
        student_hash = tokenizer_hashes[f"student_{artifact}"]
        if teacher_hash and student_hash and teacher_hash != student_hash:
            blockers.append(f"教师与学生的 {artifact} 内容不一致，token id 语义可能错位")

    gkd_trainer = Path("ms-swift/swift/rlhf_trainers/gkd_trainer.py")
    if not gkd_trainer.exists():
        blockers.append("缺少 ms-swift GKD trainer")
    else:
        trainer_text = gkd_trainer.read_text(encoding="utf-8")
        for capability in ("teacher_model_server", "gkd_logits_topk", "generate_on_policy_outputs"):
            if capability not in trainer_text:
                blockers.append(f"当前 GKD trainer 缺少能力: {capability}")

    report = {
        "status": "BLOCKED" if blockers else "PASS",
        "sample_counts": {"train": len(train), "dev": len(dev)},
        "trajectory_counts": {"train": len(train_ids), "dev": len(dev_ids)},
        "decision_counts": dict(decision_counts),
        "teacher": {"path": str(paths["teacher"]), "config": teacher_cfg},
        "student": {"path": str(paths["student"]), "config": student_cfg},
        "tokenizer_hashes": tokenizer_hashes,
        "blockers": blockers,
        "warnings": warnings,
        "scope_note": "该阶段验证离线状态分布上的逐状态 OPD；端到端交互式工具 rollout 仍是后续阶段。",
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
