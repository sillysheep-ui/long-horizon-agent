BASE_DIR="${BASE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "${BASE_DIR}"

# --model_dir "pretrained/Qwen/Qwen3-4B-Instruct-2507/" \
# --model_dir "pretrained/Qwen/Qwen3-14B/" \
# --model_dir "saved/output/qwen3_4b_sft_4gpu/v8-20260511-142452/qwen3_4b_sft_merged_420/" \
python3 -m inference.run_tool_loop_infer \
  --infer_backend transformers \
  --dataset_path  ${BASE_DIR}/data/final/test_final.jsonl \
  --model_dir "saved/output/grpo_parser_aligned_run/v8-20260514-140934/checkpoint-150/" \
  --tools_dir ${BASE_DIR}/tools/ \
  --start_idx 0 \
  --num_samples 80 \
  --max_turns 13 \
  --max_new_tokens 5000 \
  --temperature 0.2 \
  --top_p 0.95 \
  --top_k 50 \
  --tool_first_enforce \
  --system_max_tool_calls 13 \
  --max_same_tool_call_rounds 3 \
  --save_full_messages \
  --output_path ${BASE_DIR}/output_qwen3_4b_grpo_epoch150.jsonl
