#!/usr/bin/env bash
set -euo pipefail

if [[ -d /root/miniconda3/bin ]]; then
  export PATH="/root/miniconda3/bin:${PATH}"
fi
if ! [[ "${OMP_NUM_THREADS:-}" =~ ^[1-9][0-9]*$ ]]; then
  export OMP_NUM_THREADS=8
fi

PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
TEACHER_MODEL="${TEACHER_MODEL:-${PROJECT_ROOT}/saved/output/teacher_grpo_formal20_8gpu80gb_checkpoint20_merged_20260820}"
TEACHER_GPU="${TEACHER_GPU:-0}"
TEACHER_PORT="${TEACHER_PORT:-8100}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-16384}"
TEACHER_GPU_MEMORY_UTILIZATION="${TEACHER_GPU_MEMORY_UTILIZATION:-0.82}"

cd "${PROJECT_ROOT}"
test -f "${TEACHER_MODEL}/config.json"
command -v nvidia-smi >/dev/null
nvidia-smi -L | grep -q GPU

exec env CUDA_VISIBLE_DEVICES="${TEACHER_GPU}" vllm serve "${TEACHER_MODEL}" \
  --served-model-name travel-grpo-teacher \
  --host 127.0.0.1 \
  --port "${TEACHER_PORT}" \
  --tensor-parallel-size 1 \
  --dtype bfloat16 \
  --max-model-len "${MAX_MODEL_LEN}" \
  --max-logprobs 64 \
  --gpu-memory-utilization "${TEACHER_GPU_MEMORY_UTILIZATION}" \
  --enable-prefix-caching
