# -*- coding: utf-8 -*-
# --------------------------------------------
# 文件描述: 用户追问式多轮交互（含长短期记忆）
# 功能:
#   1. 交互式对话 — 用户可以多轮追问、补充约束、调整方案
#   2. 长短期记忆 — 每轮 tool loop 结束后自动压缩为摘要（长期记忆），
#                   当前轮次保留完整 tool loop（短期记忆）
#   3. 支持 transformers / vllm 两种推理后端
# --------------------------------------------

import argparse
import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List

from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

# ---------- 从 run_tool_loop_infer 复用全部核心函数 ----------

from prompts.prompt import MEMORY_SYSTEM_PROMPT, MEMORY_USER_PROMPT

from inference.run_tool_loop_infer import (
    infer,
    init_backend,
    load_system_prompt_from_dataset,
    normalize_system_prompt_inplace,
    tool_loop,
)


# ---------- 记忆压缩 ----------

def _compress_memory(tokenizer, model_or_llm, args_proxy,
                     long_term_memory: str,
                     user_input: str,
                     answer: str) -> str:
    """用推理模型压缩本轮对话为记忆摘要"""
    if not answer:
        return long_term_memory

    prompt = MEMORY_USER_PROMPT.format(
        long_term_memory=long_term_memory or "（首轮对话，无历史记忆）",
        user_input=user_input[:2000],
        answer=answer[:3000],
    )
    messages = [
        {"role": "system", "content": MEMORY_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]

    # 用低温度生成摘要 — 复用 run_tool_loop_infer.infer
    compress_args = SimpleNamespace(
        max_new_tokens=1024,
        do_sample=True,
        temperature=0.3,
        top_p=0.9,
        top_k=20,
        infer_backend=args_proxy.infer_backend,
    )
    return infer(model_or_llm, tokenizer, messages, compress_args).strip()


# ---------- 消息构建 ----------

def _build_messages(system_prompt: str,
                    long_term_memory: str,
                    user_input: str) -> List[Dict[str, Any]]:
    """构建本轮 tool loop 的初始消息"""
    messages = [{"role": "system", "content": system_prompt}]

    if long_term_memory:
        messages.append({
            "role": "user",
            "content": f"[以下是之前对话的记忆摘要]\n{long_term_memory}",
        })

    messages.append({"role": "user", "content": user_input})
    return messages


# ---------- 命令行参数 ----------

def _parse_args():
    parser = argparse.ArgumentParser(
        description="旅行规划 Agent 交互式多轮对话（含长短期记忆）"
    )
    parser.add_argument("--infer_backend", default="transformers",
                        choices=["transformers", "vllm"])
    parser.add_argument("--model_dir", required=True, help="模型路径")
    parser.add_argument("--tools_dir", default="tools", help="工具目录")
    parser.add_argument("--max_turns", type=int, default=13,
                        help="每轮追问最多工具调用数")
    parser.add_argument("--max_new_tokens", type=int, default=5000)
    parser.add_argument("--do_sample", action="store_true", default=True)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--top_p", type=float, default=0.95)
    parser.add_argument("--top_k", type=int, default=50)
    parser.add_argument("--print_chars", type=int, default=1800,
                        help="终端打印最大字符数")
    parser.add_argument("--tool_response_max_chars", type=int, default=5000)
    parser.add_argument("--system_max_tool_calls", type=int, default=13)
    parser.add_argument("--force_answer_after_turns", type=int, default=12)
    parser.add_argument("--max_no_tool_no_answer_retries", type=int, default=2)
    parser.add_argument("--max_invalid_tool_rounds", type=int, default=3)
    parser.add_argument("--max_same_tool_call_rounds", type=int, default=3)
    parser.add_argument("--override_system_prompt_file", default="")
    parser.add_argument("--override_system_prompt_idx", type=int, default=0)
    parser.add_argument("--vllm_tensor_parallel_size", type=int, default=1)
    parser.add_argument("--vllm_gpu_memory_utilization", type=float, default=0.7)
    parser.add_argument("--vllm_max_model_len", type=int, default=32000)
    parser.add_argument(
        "--tool_first_enforce",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="必须先用工具获取事实才能输出 answer",
    )
    return parser.parse_args()


# ---------- 帮助说明 ----------

HELP_TEXT = """
┌──────────────────────────────────────────────────────────┐
│              旅行规划 Agent 交互式对话                      │
│                                                          │
│  可用命令:                                                │
│    exit / quit / 退出    — 结束对话                        │
│    reset / 重置          — 清空全部对话历史（保留 system）    │
│    history / 历史        — 查看当前长期记忆摘要              │
│    help / 帮助           — 显示本帮助                       │
│                                                          │
│  直接输入出行规划问题即可开始对话。                            │
│  给出方案后可以继续追问、补充约束、否定方案。                   │
└──────────────────────────────────────────────────────────┘
"""


# ---------- 主循环 ----------

async def main():
    args = _parse_args()

    # 创建 args_proxy（SimpleNamespace 供所有函数使用）
    args_proxy = SimpleNamespace(**vars(args))

    # 初始化模型
    print("正在加载模型...")
    tokenizer, model, llm = init_backend(args)
    model_or_llm = llm if args.infer_backend == "vllm" else model
    print("模型加载完成。\n")

    # 加载 system prompt
    if args.override_system_prompt_file:
        system_prompt = load_system_prompt_from_dataset(
            args.override_system_prompt_file,
            args.override_system_prompt_idx,
        )
    else:
        from prompts.prompt import COLDSTART_SYSTEM_PROMPT
        from datetime import date
        system_prompt = (
            COLDSTART_SYSTEM_PROMPT
            .replace("__CURRENT_DATE__", date.today().strftime("%Y-%m-%d"))
            .replace("__MAX_TOOL_CALL__", str(args.system_max_tool_calls))
        )

    long_term_memory = ""
    turn_idx = 0

    print(HELP_TEXT)

    while True:
        # ── 读取用户输入 ──
        try:
            user_input = input("👤 你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break

        if not user_input:
            continue

        # ── 内置命令 ──
        if user_input.lower() in ("exit", "quit", "退出"):
            print("再见！")
            break

        if user_input.lower() in ("reset", "重置"):
            long_term_memory = ""
            turn_idx = 0
            print("[系统] 对话历史已清空，长期记忆已重置。")
            continue

        if user_input.lower() in ("history", "历史"):
            if long_term_memory:
                print(f"\n📋 长期记忆:\n{long_term_memory}\n")
            else:
                print("\n[系统] 暂无长期记忆。\n")
            continue

        if user_input.lower() in ("help", "帮助"):
            print(HELP_TEXT)
            continue

        # ── 正常追问：构建消息并进入 tool loop ──
        turn_idx += 1
        messages = _build_messages(system_prompt, long_term_memory, user_input)
        normalize_system_prompt_inplace(messages, args.system_max_tool_calls)

        print(f"\n🔄 第 {turn_idx} 轮对话 — 正在规划中...\n")

        # 使用 run_tool_loop_infer.tool_loop 执行
        loop_result = await tool_loop(
            messages, args_proxy, tokenizer, model_or_llm,
            args.tools_dir,
        )

        final_answer = loop_result["final_answer"]
        status = loop_result["status"]
        tool_count = loop_result["total_tool_calls"]

        # ── 打印结果 ──
        if final_answer:
            print(f"\n🤖 Agent:\n{final_answer[:args.print_chars]}")
            if len(final_answer) > args.print_chars:
                print(f"...[共 {len(final_answer)} 字符，仅显示前 {args.print_chars}]")
        else:
            print(f"\n[系统] 本轮未生成有效回答（状态: {status}）。")
            last_content = messages[-1].get("content", "")
            if last_content:
                print(f"最后模型输出:\n{last_content[:500]}")

        answer = final_answer
        print(f"\n[本轮统计] 状态: {status} | 工具调用: {tool_count} 次\n")

        # ── 记忆压缩 ──
        if answer:
            print("🧠 正在更新长期记忆...")
            new_memory = _compress_memory(
                tokenizer, model_or_llm, args_proxy,
                long_term_memory, user_input, answer,
            )
            if new_memory:
                long_term_memory = new_memory
                print("   ✓ 记忆已更新")
        print()

        # ── 清理本轮 tool loop 的详细消息（只保留压缩后的摘要） ──
        # messages 在本轮结束后不再需要，下次追问时会用 _build_messages 重建


if __name__ == "__main__":
    asyncio.run(main())
