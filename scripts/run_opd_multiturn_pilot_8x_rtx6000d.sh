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
TEACHER_MODEL="${TEACHER_MODEL:-${PROJECT_ROOT}/saved/output/teacher_grpo_formal20_8gpu80gb_checkpoint20_merged_20260820}"
PLUGIN="${PLUGIN:-${PROJECT_ROOT}/ms-swift/examples/train/grpo/plugin/tooluse_multi_turn_scheduler.py}"
TRAIN_DATA="${TRAIN_DATA:-${PROJECT_ROOT}/data/final/opd_pilot_20260822/train_mixed.jsonl}"
OUTPUT_ROOT="${OUTPUT_ROOT:-${PROJECT_ROOT}/saved/output/opd_multiturn_pilot_50step_20260822}"
TEACHER_PORT="${TEACHER_PORT:-8100}"
STUDENT_PORT="${STUDENT_PORT:-8001}"
MAX_STEPS="${MAX_STEPS:-50}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-32768}"
MAX_COMPLETION_LENGTH="${MAX_COMPLETION_LENGTH:-2048}"
MAX_TURNS="${MAX_TURNS:-7}"
TEACHER_GPU_MEMORY_UTILIZATION="${TEACHER_GPU_MEMORY_UTILIZATION:-0.68}"
STUDENT_GPU_MEMORY_UTILIZATION="${STUDENT_GPU_MEMORY_UTILIZATION:-0.62}"

cd "${PROJECT_ROOT}"
mkdir -p "${OUTPUT_ROOT}/logs"
TEACHER_LOG="${OUTPUT_ROOT}/logs/teacher_server.log"
STUDENT_LOG="${OUTPUT_ROOT}/logs/student_rollout.log"
TRAIN_LOG="${OUTPUT_ROOT}/logs/train.log"
TEACHER_PID=""
STUDENT_PID=""

stop_group() {
  local group_pid="$1"
  if [[ -z "${group_pid}" ]]; then
    return
  fi
  kill -TERM -- "-${group_pid}" 2>/dev/null || true
  for _ in $(seq 1 10); do
    if ! kill -0 -- "-${group_pid}" 2>/dev/null; then
      return
    fi
    sleep 1
  done
  kill -KILL -- "-${group_pid}" 2>/dev/null || true
}

cleanup() {
  local exit_code=$?
  trap - EXIT INT TERM
  stop_group "${STUDENT_PID}"
  stop_group "${TEACHER_PID}"
  wait "${STUDENT_PID}" 2>/dev/null || true
  wait "${TEACHER_PID}" 2>/dev/null || true
  echo "GPU processes after cleanup:"
  nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader || true
  exit "${exit_code}"
}
trap cleanup EXIT INT TERM

wait_for_url() {
  local url="$1"
  local process_pid="$2"
  local log_file="$3"
  local label="$4"
  for _ in $(seq 1 180); do
    if curl --fail --silent "${url}" >/dev/null; then
      echo "${label} ready: ${url}"
      return
    fi
    if ! kill -0 "${process_pid}" 2>/dev/null; then
      echo "${label} exited before becoming ready: ${log_file}" >&2
      tail -n 100 "${log_file}" >&2 || true
      return 1
    fi
    sleep 2
  done
  echo "${label} was not ready within 6 minutes: ${log_file}" >&2
  return 1
}

test -f "${STUDENT_MODEL}/config.json"
test -f "${TEACHER_MODEL}/config.json"
test -f "${PLUGIN}"
test -s "${TRAIN_DATA}"
GPU_COUNT="$(nvidia-smi --query-gpu=index --format=csv,noheader | wc -l | tr -d ' ')"
if (( GPU_COUNT != 8 )); then
  echo "This pilot requires exactly 8 GPUs; detected ${GPU_COUNT}." >&2
  exit 1
fi
MIN_GPU_MEMORY_MIB="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | sort -n | head -1 | tr -d ' ')"
if (( MIN_GPU_MEMORY_MIB < 80000 )); then
  echo "Each GPU must provide at least 80,000 MiB; minimum detected ${MIN_GPU_MEMORY_MIB}." >&2
  exit 1
fi
if [[ -n "$(nvidia-smi --query-compute-apps=pid --format=csv,noheader,nounits 2>/dev/null | tr -d '[:space:]')" ]]; then
  echo "GPU processes already exist; refusing to mix workloads." >&2
  nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader >&2 || true
  exit 1
fi
AVAILABLE_GIB="$(df -BG --output=avail "${PROJECT_ROOT}" | tail -1 | tr -dc '0-9')"
if (( AVAILABLE_GIB < 25 )); then
  echo "At least 25 GiB free disk is required; detected ${AVAILABLE_GIB} GiB." >&2
  exit 1
fi

python3 -m data_pipeline.build_opd_pilot_curriculum
python3 -m tests.test_travel_multi_turn_scheduler

export TRAVEL_AGENTIC_RL_ROOT="${PROJECT_ROOT}"
export TOOL_LOOP_TOOLS_DIR="${PROJECT_ROOT}/tools"
export TOOL_LOOP_TOOL_RESPONSE_MAX_CHARS="${TOOL_LOOP_TOOL_RESPONSE_MAX_CHARS:-1200}"
export TOOL_LOOP_SYSTEM_MAX_TOOL_CALLS="${MAX_TURNS}"
export TOOL_LOOP_FORCE_ANSWER_AFTER_TURNS="${TOOL_LOOP_FORCE_ANSWER_AFTER_TURNS:-6}"
export TOOL_LOOP_MAX_CONTEXT_TOKENS="${TOOL_LOOP_MAX_CONTEXT_TOKENS:-${MAX_MODEL_LEN}}"
export TOOL_LOOP_FINAL_ANSWER_RESERVE_TOKENS="${TOOL_LOOP_FINAL_ANSWER_RESERVE_TOKENS:-3072}"
export TOOL_LOOP_COMPACTED_TOOL_MAX_CHARS="${TOOL_LOOP_COMPACTED_TOOL_MAX_CHARS:-256}"
export SWIFT_GKD_ALLOW_MULTI_TURN=1

setsid env \
  TEACHER_MODEL="${TEACHER_MODEL}" \
  TEACHER_GPU=0 \
  TEACHER_PORT="${TEACHER_PORT}" \
  MAX_MODEL_LEN="${MAX_MODEL_LEN}" \
  TEACHER_GPU_MEMORY_UTILIZATION="${TEACHER_GPU_MEMORY_UTILIZATION}" \
  bash run_opd_teacher_server.sh >"${TEACHER_LOG}" 2>&1 &
TEACHER_PID=$!
wait_for_url "http://127.0.0.1:${TEACHER_PORT}/health" "${TEACHER_PID}" "${TEACHER_LOG}" "teacher"

setsid env CUDA_VISIBLE_DEVICES=1 swift rollout \
  --model "${STUDENT_MODEL}" \
  --external_plugins "${PLUGIN}" \
  --multi_turn_scheduler travel_tool_loop \
  --vllm_use_async_engine true \
  --vllm_max_model_len "${MAX_MODEL_LEN}" \
  --vllm_tensor_parallel_size 1 \
  --vllm_gpu_memory_utilization "${STUDENT_GPU_MEMORY_UTILIZATION}" \
  --vllm_engine_kwargs '{"load_format":"auto"}' \
  --max_turns "${MAX_TURNS}" \
  --port "${STUDENT_PORT}" >"${STUDENT_LOG}" 2>&1 &
STUDENT_PID=$!
wait_for_url "http://127.0.0.1:${STUDENT_PORT}/health/" "${STUDENT_PID}" "${STUDENT_LOG}" "student rollout"

env \
  NPROC_PER_NODE=6 \
  CUDA_VISIBLE_DEVICES=2,3,4,5,6,7 \
  PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
  swift rlhf \
    --rlhf_type gkd \
    --model "${STUDENT_MODEL}" \
    --teacher_model_server "http://127.0.0.1:${TEACHER_PORT}" \
    --gkd_logits_topk 64 \
    --tuner_type full \
    --dataset "${TRAIN_DATA}" \
    --dataset_shuffle false \
    --split_dataset_ratio 0 \
    --seq_kd false \
    --lmbda 1 \
    --beta 1 \
    --temperature 1.0 \
    --sft_alpha 0 \
    --torch_dtype bfloat16 \
    --max_steps "${MAX_STEPS}" \
    --per_device_train_batch_size 1 \
    --gradient_accumulation_steps 1 \
    --num_generations 1 \
    --learning_rate 1e-6 \
    --warmup_ratio 0.05 \
    --logging_steps 1 \
    --save_steps 10 \
    --save_total_limit 5 \
    --save_only_model true \
    --max_length "${MAX_MODEL_LEN}" \
    --max_completion_length "${MAX_COMPLETION_LENGTH}" \
    --completion_length_limit_scope total \
    --output_dir "${OUTPUT_ROOT}/checkpoints" \
    --dataloader_num_workers 0 \
    --dataset_num_proc 1 \
    --deepspeed zero2 \
    --attn_impl sdpa \
    --gradient_checkpointing true \
    --use_vllm true \
    --vllm_mode server \
    --vllm_server_host 127.0.0.1 \
    --vllm_server_port "${STUDENT_PORT}" \
    --vllm_server_timeout 900 \
    --external_plugins "${PLUGIN}" \
    --multi_turn_scheduler travel_tool_loop \
    --max_turns "${MAX_TURNS}" \
    --vllm_server_pass_dataset true \
    --report_to tensorboard 2>&1 | tee "${TRAIN_LOG}"

echo "Pilot completed. Artifacts: ${OUTPUT_ROOT}"
