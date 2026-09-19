#!/usr/bin/env python3
"""在冻结的 Qwen3 主模型上训练独立 AgentPRM LoRA。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

import torch
from datasets import Dataset
from peft import LoraConfig, get_peft_model
from transformers import AutoModelForCausalLM, AutoTokenizer, Trainer, TrainingArguments


def load_jsonl(path: str) -> Dataset:
    return Dataset.from_list([json.loads(line) for line in Path(path).open(encoding="utf-8") if line.strip()])


def tokenize_row(row, tokenizer, max_length: int):
    messages = row["conversations"]
    def token_ids(chat, add_generation_prompt: bool):
        encoded = tokenizer.apply_chat_template(chat, tokenize=True, add_generation_prompt=add_generation_prompt)
        if hasattr(encoded, "input_ids"):
            encoded = encoded.input_ids
        elif isinstance(encoded, dict):
            encoded = encoded["input_ids"]
        if encoded and isinstance(encoded[0], list):
            encoded = encoded[0]
        return list(encoded)
    prefix = token_ids(messages[:-1], True)
    full = token_ids(messages, False)
    labels = [-100] * len(prefix) + full[len(prefix):]
    input_ids = full
    if len(input_ids) > max_length:
        input_ids, labels = input_ids[-max_length:], labels[-max_length:]
    return {"input_ids": input_ids, "attention_mask": [1] * len(input_ids), "labels": labels}


@dataclass
class Collator:
    pad_token_id: int

    def __call__(self, features):
        max_len = max(len(item["input_ids"]) for item in features)
        ids, masks, labels = [], [], []
        for item in features:
            pad = max_len - len(item["input_ids"])
            ids.append(item["input_ids"] + [self.pad_token_id] * pad)
            masks.append(item["attention_mask"] + [0] * pad)
            labels.append(item["labels"] + [-100] * pad)
        return {"input_ids": torch.tensor(ids), "attention_mask": torch.tensor(masks), "labels": torch.tensor(labels)}


def main(args):
    tokenizer = AutoTokenizer.from_pretrained(args.model_dir, trust_remote_code=True)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    train = load_jsonl(args.train).map(lambda x: tokenize_row(x, tokenizer, args.max_length), remove_columns=["id", "conversations", "metadata"])
    val = load_jsonl(args.val).map(lambda x: tokenize_row(x, tokenizer, args.max_length), remove_columns=["id", "conversations", "metadata"])
    model = AutoModelForCausalLM.from_pretrained(args.model_dir, torch_dtype=torch.bfloat16, trust_remote_code=True)
    model.config.use_cache = False
    model = get_peft_model(model, LoraConfig(
        r=args.rank, lora_alpha=args.rank * 2, lora_dropout=0.05, bias="none", task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
    ))
    model.print_trainable_parameters()
    train_args = TrainingArguments(
        output_dir=args.output_dir, num_train_epochs=args.epochs, learning_rate=args.learning_rate,
        per_device_train_batch_size=1, per_device_eval_batch_size=1, gradient_accumulation_steps=args.grad_accum,
        gradient_checkpointing=True, bf16=True, logging_steps=10, eval_strategy="epoch", save_strategy="epoch",
        save_total_limit=2, report_to="none", remove_unused_columns=False, seed=42,
    )
    trainer = Trainer(model=model, args=train_args, train_dataset=train, eval_dataset=val, data_collator=Collator(tokenizer.pad_token_id))
    trainer.train()
    trainer.save_model(args.output_dir)
    tokenizer.save_pretrained(args.output_dir)
    metrics = trainer.evaluate()
    Path(args.output_dir, "final_metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", default="model/checkpoint-150")
    parser.add_argument("--train", default="data/final/agent_prm_v2_train.jsonl")
    parser.add_argument("--val", default="data/final/agent_prm_v2_val.jsonl")
    parser.add_argument("--output-dir", default="saved/agentprm_v2_lora")
    parser.add_argument("--max-length", type=int, default=4096)
    parser.add_argument("--rank", type=int, default=16)
    parser.add_argument("--epochs", type=float, default=3.0)
    parser.add_argument("--learning-rate", type=float, default=2e-4)
    parser.add_argument("--grad-accum", type=int, default=8)
    main(parser.parse_args())
