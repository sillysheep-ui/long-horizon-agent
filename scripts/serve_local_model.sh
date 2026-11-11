#!/usr/bin/env bash
# 启动本地 Qwen3-4B（MLX）的 OpenAI 兼容服务，供产品层 local_first 路由使用。
set -euo pipefail

MODEL_DIR="${1:-${LOCAL_MODEL_PATH:-}}"
if [[ -z "$MODEL_DIR" ]]; then
  echo "用法: scripts/serve_local_model.sh <模型目录>" >&2
  exit 2
fi
if [[ ! -f "$MODEL_DIR/config.json" ]]; then
  echo "不是 HuggingFace 模型目录（缺 config.json）: $MODEL_DIR" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${VENV:-$ROOT/.venv-local}"
if [[ ! -x "$VENV/bin/mlx_lm.server" ]]; then
  UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/uv-cache}" uv venv "$VENV"
  UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/uv-cache}" uv pip install --python "$VENV/bin/python" mlx-lm
fi

# 本机的 anaconda 自带 MPICH，MLX 检测到后会要求 Open MPI 并直接 abort；
# 指向不存在的库名可让 distributed 退回单进程（本服务不需要分布式）。
exec env MLX_MPI_LIBNAME="${MLX_MPI_LIBNAME:-libmpi_not_here.dylib}" \
  "$VENV/bin/mlx_lm.server" \
  --model "$MODEL_DIR" \
  --host 127.0.0.1 \
  --port "${PORT:-8080}" \
  --log-level WARNING
