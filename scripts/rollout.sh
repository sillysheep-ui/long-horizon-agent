#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MS_SWIFT_DIR="${MS_SWIFT_DIR:-${PROJECT_ROOT}/ms-swift}"

# Base SFT checkpoint used for RL.
MODEL_PATH="${MODEL_PATH:-${PROJECT_ROOT}/saved/output/qwen3_4b_sft_4gpu/v8-20260511-142452/qwen3_4b_sft_merged_420}"

# Data and plugin paths.
SCHEDULER_PLUGIN="${SCHEDULER_PLUGIN:-${PROJECT_ROOT}/ms-swift/examples/train/grpo/plugin/tooluse_multi_turn_scheduler.py}"

# Rollout server config.
VLLM_GPU_MEMORY_UTILIZATION="${VLLM_GPU_MEMORY_UTILIZATION:-0.8}"

MAX_TURNS="${MAX_TURNS:-13}"
MAX_LENGTH="${MAX_LENGTH:-50000}"

# 设置正确的NCCL参数
export NCCL_IB_DISABLE=1
export NCCL_P2P_DISABLE=0
export NCCL_SOCKET_IFNAME=eth0

# start rollout server in another terminal if it is not already running.
ROLLOUT_CUDA_VISIBLE_DEVICES=0,1 \
swift rollout \
  --model "${MODEL_PATH}" \
  --external_plugins "${SCHEDULER_PLUGIN}" \
  --multi_turn_scheduler travel_tool_loop \
  --vllm_use_async_engine true \
  --vllm_max_model_len "${MAX_LENGTH}" \
  --vllm_tensor_parallel_size 2 \
  --vllm_gpu_memory_utilization "${VLLM_GPU_MEMORY_UTILIZATION}" \
  --max_turns "${MAX_TURNS}"
