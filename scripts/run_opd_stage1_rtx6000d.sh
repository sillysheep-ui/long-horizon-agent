#!/usr/bin/env bash
set -euo pipefail

if [[ -d /root/miniconda3/bin ]]; then
  export PATH="/root/miniconda3/bin:${PATH}"
fi
if ! [[ "${OMP_NUM_THREADS:-}" =~ ^[1-9][0-9]*$ ]]; then
  export OMP_NUM_THREADS=8
fi

# RTX 6000D 专用入口：1 张教师卡，其余最多 3 张学生卡。
PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
TEACHER_PORT="${TEACHER_PORT:-8100}"
MAX_STEPS="${MAX_STEPS:-20}"
LOG_DIR="${LOG_DIR:-${PROJECT_ROOT}/saved/output/opd_stage1_rtx6000d_logs}"

cd "${PROJECT_ROOT}"

GPU_COUNT="$(nvidia-smi --query-gpu=index --format=csv,noheader | wc -l | tr -d ' ')"
if (( GPU_COUNT < 2 )); then
  echo "RTX 6000D OPD 至少需要 2 张 GPU：1 张教师 + 1 张学生" >&2
  exit 1
fi

python3 -m training.opd_rtx6000d_preflight --min-gpus 2 --min-memory-gib 80

STUDENT_NPROC=$((GPU_COUNT - 1))
if (( STUDENT_NPROC > 3 )); then
  STUDENT_NPROC=3
fi
STUDENT_GPUS="$(seq -s, 1 "${STUDENT_NPROC}")"

if python3 -c 'import flash_attn' >/dev/null 2>&1; then
  ATTN_IMPL="${ATTN_IMPL:-flash_attn}"
else
  ATTN_IMPL="${ATTN_IMPL:-sdpa}"
fi

mkdir -p "${LOG_DIR}"
TEACHER_LOG="${LOG_DIR}/teacher.log"

TEACHER_PID=""
cleanup() {
  if [[ -n "${TEACHER_PID}" ]] && kill -0 "${TEACHER_PID}" 2>/dev/null; then
    kill "${TEACHER_PID}" 2>/dev/null || true
    wait "${TEACHER_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

TEACHER_GPU=0 TEACHER_PORT="${TEACHER_PORT}" \
  bash run_opd_teacher_server.sh >"${TEACHER_LOG}" 2>&1 &
TEACHER_PID=$!

READY=0
for _ in $(seq 1 180); do
  if curl --fail --silent "http://127.0.0.1:${TEACHER_PORT}/v1/models" >/dev/null; then
    READY=1
    break
  fi
  if ! kill -0 "${TEACHER_PID}" 2>/dev/null; then
    echo "教师服务提前退出，日志位于 ${TEACHER_LOG}" >&2
    tail -n 80 "${TEACHER_LOG}" >&2 || true
    exit 1
  fi
  sleep 2
done
if (( READY == 0 )); then
  echo "教师服务在 6 分钟内未就绪，日志位于 ${TEACHER_LOG}" >&2
  exit 1
fi

echo "RTX 6000D 拓扑：GPU 0=教师，GPU ${STUDENT_GPUS}=学生，attention=${ATTN_IMPL}"
STUDENT_GPUS="${STUDENT_GPUS}" \
NPROC_PER_NODE="${STUDENT_NPROC}" \
TEACHER_PORT="${TEACHER_PORT}" \
MAX_STEPS="${MAX_STEPS}" \
ATTN_IMPL="${ATTN_IMPL}" \
bash run_opd_stage1_smoke.sh
