#!/usr/bin/env bash
set -euo pipefail

# 加载环境变量
source .env
export JUDGE_API_KEY=${OPENAI_API_KEY}
export JUDGE_BASE_URL=${OPENAI_BASE_URL}
export JUDGE_MODEL=${JUDGE_MODEL_ID}


# 设置正确的NCCL参数
export NCCL_IB_DISABLE=1
export NCCL_P2P_DISABLE=0
export NCCL_SOCKET_IFNAME=eth0

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MS_SWIFT_DIR="${MS_SWIFT_DIR:-${PROJECT_ROOT}/ms-swift}"

# Base SFT checkpoint used for RL.
MODEL_PATH="${MODEL_PATH:-${PROJECT_ROOT}/saved/output/qwen3_4b_sft_4gpu/v8-20260511-142452/qwen3_4b_sft_merged_420}"
REF_MODEL_PATH="${REF_MODEL_PATH:-${MODEL_PATH}}"

# Data and plugin paths.
RL_DATASET="${RL_DATASET:-${PROJECT_ROOT}/data/final/rl.jsonl}"
ANSWER_JUDGE_GOLD_DATASET_PATH="${ANSWER_JUDGE_GOLD_DATASET_PATH:-${PROJECT_ROOT}/data/final/rl.jsonl}"
REWARD_PLUGIN="${REWARD_PLUGIN:-${PROJECT_ROOT}/ms-swift/examples/train/grpo/plugin/tooluse_reward_parser_aligned.py}"
SCHEDULER_PLUGIN="${SCHEDULER_PLUGIN:-${PROJECT_ROOT}/ms-swift/examples/train/grpo/plugin/tooluse_multi_turn_scheduler.py}"

# Output and distributed training.
OUTPUT_DIR="${OUTPUT_DIR:-${PROJECT_ROOT}/saved/output/grpo_parser_aligned_run}"
TRAIN_CUDA_VISIBLE_DEVICES="${TRAIN_CUDA_VISIBLE_DEVICES:-2,3,4,5,6,7}"
NPROC_PER_NODE="${NPROC_PER_NODE:-6}"
MASTER_PORT="${MASTER_PORT:-29511}"

# Rollout server config.
VLLM_SERVER_HOST="${VLLM_SERVER_HOST:-127.0.0.1}"
VLLM_SERVER_PORT="${VLLM_SERVER_PORT:-8000}"
VLLM_GPU_MEMORY_UTILIZATION="${VLLM_GPU_MEMORY_UTILIZATION:-0.8}"
MAX_TURNS="${MAX_TURNS:-13}"

# Training hyperparameters.
MAX_STEPS="${MAX_STEPS:-200}"
SAVE_STEPS="${SAVE_STEPS:-50}"
LEARNING_RATE="${LEARNING_RATE:-1e-6}"
TOOL_LOOP_SYSTEM_MAX_TOOL_CALLS="${TOOL_LOOP_SYSTEM_MAX_TOOL_CALLS:-13}"
PER_DEVICE_TRAIN_BATCH_SIZE="${PER_DEVICE_TRAIN_BATCH_SIZE:-1}"
GRADIENT_ACCUMULATION_STEPS="${GRADIENT_ACCUMULATION_STEPS:-1}"
NUM_GENERATIONS="${NUM_GENERATIONS:-${NPROC_PER_NODE}}"
TEMPERATURE="${TEMPERATURE:-0.9}"
MAX_LENGTH="${MAX_LENGTH:-50000}"
MAX_COMPLETION_LENGTH="${MAX_COMPLETION_LENGTH:-5000}"

# Reward / judge env vars read by the custom plugin.
export ANSWER_JUDGE_GOLD_DATASET_PATH
echo $ANSWER_JUDGE_GOLD_DATASET_PATH
export JUDGE_API_KEY="${JUDGE_API_KEY:-}"
export JUDGE_BASE_URL="${JUDGE_BASE_URL:-}"
export JUDGE_MODEL="${JUDGE_MODEL:-}"
export JUDGE_TIMEOUT_SEC="${JUDGE_TIMEOUT_SEC:-30}"

export PARSER_REWARD_TOTAL_STEPS="${PARSER_REWARD_TOTAL_STEPS:-${MAX_STEPS}}"
export PARSER_REWARD_PHASE_RATIOS="${PARSER_REWARD_PHASE_RATIOS:-[0.15, 0.2, 0.65]}"
export PARSER_REWARD_W1="${PARSER_REWARD_W1:-[0.10, 0.10, 0.05, 0.05, 0.10, 0.60]}"
export PARSER_REWARD_W2="${PARSER_REWARD_W2:-[0.05, 0.05, 0.05, 0.05, 0.10, 0.70]}"
export PARSER_REWARD_W3="${PARSER_REWARD_W3:-[0.05, 0.05, 0.05, 0.05, 0.05, 0.75]}"
export PARSER_REWARD_DEBUG="${PARSER_REWARD_DEBUG:-1}"

cd "${MS_SWIFT_DIR}"

# run GRPO training.
CUDA_VISIBLE_DEVICES="${TRAIN_CUDA_VISIBLE_DEVICES}" \
NPROC_PER_NODE="${NPROC_PER_NODE}" \
MASTER_PORT="${MASTER_PORT}" \
swift rlhf \
  --rlhf_type grpo \
  --model "${MODEL_PATH}" \
  --ref_model "${REF_MODEL_PATH}" \
  --dataset "${RL_DATASET}" \
  --external_plugins "${REWARD_PLUGIN}" "${SCHEDULER_PLUGIN}" \
  --reward_funcs external_parser_aligned_curriculum_reward \
  --use_vllm true \
  --vllm_mode server \
  --vllm_server_host "${VLLM_SERVER_HOST}" \
  --vllm_server_port "${VLLM_SERVER_PORT}" \
  --vllm_gpu_memory_utilization "${VLLM_GPU_MEMORY_UTILIZATION}" \
  --vllm_tensor_parallel_size 2 \
  --vllm_enable_prefix_caching true \
  --vllm_disable_custom_all_reduce true \
  --tuner_type full \
  --torch_dtype bfloat16 \
  --deepspeed zero3 \
  --learning_rate "${LEARNING_RATE}" \
  --max_steps "${MAX_STEPS}" \
  --save_steps "${SAVE_STEPS}" \
  --save_total_limit 2 \
  --per_device_train_batch_size "${PER_DEVICE_TRAIN_BATCH_SIZE}" \
  --gradient_accumulation_steps "${GRADIENT_ACCUMULATION_STEPS}" \
  --gradient_checkpointing true \
  --gradient_checkpointing_kwargs '{"use_reentrant": false}' \
  --max_length "${MAX_LENGTH}" \
  --max_completion_length "${MAX_COMPLETION_LENGTH}" \
  --num_generations "${NUM_GENERATIONS}" \
  --temperature "${TEMPERATURE}" \
  --top_k 50 \
  --top_p 0.9 \
  --beta 0.04 \
  --loss_type grpo \
  --multi_turn_scheduler travel_tool_loop \
  --max_turns "${MAX_TURNS}" \
  --completion_length_limit_scope per_round \
  --scale_rewards group \
  --importance_sampling_level token \
  --num_iterations 1 \
  --dataloader_num_workers 1 \
  --dataset_num_proc 1 \
  --logging_steps 1 \
  --log_completions true \
  --report_to tensorboard \
  --output_dir "${OUTPUT_DIR}"

