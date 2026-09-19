cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

swift export \
    --model "pretrained/Qwen/Qwen3-4B-Instruct-2507" \
    --adapters 'saved/output/qwen3_4b_sft_4gpu/v8-20260511-142452/checkpoint-420/' \
    --output_dir 'saved/output/qwen3_4b_sft_4gpu/v8-20260511-142452/qwen3_4b_sft_merged_420/' \
    --merge_lora true \
    --safe_serialization true
