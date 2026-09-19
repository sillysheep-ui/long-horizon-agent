#!/usr/bin/env bash
set -euo pipefail

if [[ -d /root/miniconda3/bin ]]; then
  export PATH="/root/miniconda3/bin:${PATH}"
fi
if ! [[ "${OMP_NUM_THREADS:-}" =~ ^[1-9][0-9]*$ ]]; then
  export OMP_NUM_THREADS=8
fi

PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
STUDENT_MODEL="${STUDENT_MODEL:-${PROJECT_ROOT}/saved/output/grpo_parser_aligned_run/v8-20260514-140934/checkpoint-150}"
TRAIN_DATA="${TRAIN_DATA:-${PROJECT_ROOT}/data/final/opd_stage1_16k/train.jsonl}"
DEV_DATA="${DEV_DATA:-${PROJECT_ROOT}/data/final/opd_stage1_16k/dev.jsonl}"
TEACHER_PORT="${TEACHER_PORT:-8100}"
STUDENT_GPUS="${STUDENT_GPUS:-1,2,3,4}"
NPROC_PER_NODE="${NPROC_PER_NODE:-4}"
MAX_STEPS="${MAX_STEPS:-20}"
OUTPUT_DIR="${OUTPUT_DIR:-${PROJECT_ROOT}/saved/output/qwen3_4b_grpo_opd_stage1_smoke}"
ATTN_IMPL="${ATTN_IMPL:-flash_attn}"
VLLM_GPU_MEMORY_UTILIZATION="${VLLM_GPU_MEMORY_UTILIZATION:-0.35}"

cd "${PROJECT_ROOT}"
python3 -m training.opd_stage1_preflight \
  --train "${TRAIN_DATA}" \
  --dev "${DEV_DATA}" \
  --student "${STUDENT_MODEL}"

curl --fail --silent "http://127.0.0.1:${TEACHER_PORT}/v1/models" >/dev/null
command -v nvidia-smi >/dev/null
nvidia-smi -L | grep -q GPU

CUDA_VISIBLE_DEVICES="${STUDENT_GPUS}" \
NPROC_PER_NODE="${NPROC_PER_NODE}" \
PYTORCH_CUDA_ALLOC_CONF="expandable_segments:True" \
swift rlhf \
  --rlhf_type gkd \
  --model "${STUDENT_MODEL}" \
  --tuner_type lora \
  --teacher_model_server "http://127.0.0.1:${TEACHER_PORT}" \
  --gkd_logits_topk 64 \
  --dataset "${TRAIN_DATA}" \
  --val_dataset "${DEV_DATA}" \
  --seq_kd false \
  --lmbda 1 \
  --beta 1 \
  --temperature 1.0 \
  --sft_alpha 0 \
  --use_vllm true \
  --vllm_mode colocate \
  --vllm_tensor_parallel_size 1 \
  --vllm_gpu_memory_utilization "${VLLM_GPU_MEMORY_UTILIZATION}" \
  --vllm_max_model_len 16384 \
  --max_length 16384 \
  --max_completion_length 2048 \
  --torch_dtype bfloat16 \
  --per_device_train_batch_size 1 \
  --gradient_accumulation_steps 2 \
  --learning_rate 1e-6 \
  --max_steps "${MAX_STEPS}" \
  --warmup_ratio 0.05 \
  --logging_steps 1 \
  --eval_steps 10 \
  --save_steps 10 \
  --save_total_limit 2 \
  --output_dir "${OUTPUT_DIR}" \
  --gradient_checkpointing true \
  --deepspeed zero2 \
  --attn_impl "${ATTN_IMPL}" \
  --save_only_model true \
  --report_to tensorboard
