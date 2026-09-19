#!/usr/bin/env bash
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ===== SFT config =====
MODEL_PATH="pretrained/Qwen/Qwen3-4B-Instruct-2507"
TRAIN_DATA="data/final/sft_train.jsonl"
VAL_DATA="data/final/sft_val.jsonl"
OUTPUT_DIR="saved/output/qwen3_4b_sft_4gpu"

# Multi-GPU setup
export CUDA_VISIBLE_DEVICES=0,1,2,3
export NPROC_PER_NODE=4
echo $CUDA_VISIBLE_DEVICES

swift sft \
  --model "${MODEL_PATH}" \
  --dataset "${TRAIN_DATA}" \
  --val_dataset "${VAL_DATA}" \
  --tuner_type lora \
  --lora_rank 128 \
  --num_train_epochs 2 \
  --learning_rate 8e-6 \
  --max_length 32768 \
  --per_device_train_batch_size 1 \
  --gradient_accumulation_steps 1 \
  --gradient_checkpointing true \
  --dataloader_num_workers 8 \
  --save_steps 200 \
  --eval_steps 50 \
  --logging_steps 5 \
  --output_dir "${OUTPUT_DIR}"
