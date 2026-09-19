# -*- coding: utf-8 -*-
# --------------------------------------------
# 文件描述: 在线rollout 
# --------------------------------------------

import argparse
import json_repair
import asyncio
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from dotenv import load_dotenv
from typing import Any, Dict, List, Optional, Tuple

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

# 加载环境变量
load_dotenv()


TOOL_CLASS_MAP = {
    "search": ("tool_web_search.py", "WebSearch"),
    "visit": ("tool_visit.py", "Visit"),
    "poi_search": ("tool_poi_search.py", "POISearch"),
    "weather_search": ("tool_weather.py", "WeatherSearch"),
    "around_search": ("tool_around_search.py", "AroundSearch"),
    "route_planning": ("tool_route_planning.py", "RoutePlanning"),
    "train_tickets_search": ("tool_train_ticket.py", "TrainTicketsSearch"),
    "flights_search": ("tool_transport.py", "FlightsSearch"),
    "hotel_search": ("tool_hotel.py", "HotelSearch"),
    "distance_matrix": ("tool_distance_matrix.py", "DistanceMatrix"),
    "catering_search": ("tool_catering.py", "CateringSearch"),
}


def _extract_tag_block(text: str, tag: str) -> str:
    m = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", text or "", re.DOTALL)
    if not m:
        return ""
    return m.group(1).strip()


def _extract_line(text: str, prefix: str, default: str = "") -> str:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            return stripped
    return default


def _simplify_system_prompt(system_prompt: str, max_tool_calls: int) -> str:
    tools_block = _extract_tag_block(system_prompt, "tools")
    if not tools_block:
        return system_prompt

    date_line = _extract_line(system_prompt, "当前日期：", "当前日期：未知")
    max_turns_line = (
        f"最大可调用{max_tool_calls}轮工具"
        if max_tool_calls > 0
        else _extract_line(system_prompt, "最大可调用", "最大可调用13轮工具")
    )

    concise_rules = [
        "你是旅行规划助手，需要先用工具获取事实，再给最终回答。",
        "每一轮只能二选一输出（工具阶段 或者 最终阶段），在每一轮输出前可以先结合已有信息给出思考，格式为<think>...</think>：",
        "1. 工具阶段：输出格式为<tool_call>...</tool_call>，可以输出多个工具，每一个工具的格式如下:",
        "<tool_call>",
        '{"name": <function-name>, "arguments": <args-json-object>}',
        "</tool_call>",
        "2. 最终阶段：答案输出格式为 <answer>...</answer>",

        "# HARD LIMIT",
        "2. 没有成功通过工具获取事实之前，禁止输出 <answer>。",
        "3. 如果仍需继续查询，就继续调用工具 <tool_call>。",
        "4. 如果信息已足够，就直接输出一次且仅一次 <answer>...</answer>。",
        "5. 不要在同一轮同时输出 <tool_call> 和 <answer>。",
        "6. 工具参数必须是可解析 JSON，字段名必须与工具定义一致。"
    ]

    return "\n".join(
        concise_rules
        + [
            "",
            "# Tools",
            "<tools>",
            tools_block,
            "</tools>",
            "",
            date_line,
            max_turns_line,
        ]
    )


def apply_system_tool_call_limit(system_prompt: str, max_tool_calls: int) -> str:
    if max_tool_calls > 0:
        system_prompt = re.sub(
            r"最大可调用\s*\d+\s*轮工具",
            f"最大可调用{max_tool_calls}轮工具",
            system_prompt,
        )
    if os.getenv("TOOL_LOOP_SIMPLIFY_SYSTEM_PROMPT", "1") != "0":
        system_prompt = _simplify_system_prompt(system_prompt, max_tool_calls)

    return system_prompt


def normalize_system_prompt_inplace(messages: List[Dict[str, Any]], max_tool_calls: int) -> bool:
    if not messages or messages[0].get("role") != "system":
        return False
    messages[0] = {
        "role": "system",
        "content": apply_system_tool_call_limit(
            str(messages[0].get("content", "")),
            max_tool_calls,
        ),
    }
    return True


def load_jsonl_row(path: str, idx: int) -> Dict[str, Any]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    if idx < 0 or idx >= len(lines):
        raise IndexError(f"sample_idx {idx} out of range, total: {len(lines)}")
    return json.loads(lines[idx])


def load_system_prompt_from_dataset(path: str, idx: int = 0) -> str:
    row = load_jsonl_row(path, idx)
    conversations = row.get("conversations") or row.get("messages") or row.get("answer")
    if not conversations:
        raise ValueError(f"No conversations/messages/answer found in: {path}")
    first = conversations[0]
    if first.get("role") != "system":
        raise ValueError(f"First role is not system in: {path} idx={idx}")
    return str(first.get("content", ""))


def load_tool(tools_dir: str, tool_name: str):
    if tool_name not in TOOL_CLASS_MAP:
        return None
    module_rel, cls_name = TOOL_CLASS_MAP[tool_name]
    module_path = str(Path(tools_dir) / module_rel)
    spec = importlib.util.spec_from_file_location("tool_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    cls = getattr(module, cls_name)
    return cls()


def parse_tool_calls(text: str) -> List[Dict[str, Any]]:
    def _normalize_calls(payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, dict):
            # OpenAI style: {"tool_calls":[...]}
            if isinstance(payload.get("tool_calls"), list):
                return [c for c in payload["tool_calls"] if isinstance(c, dict)]
            # Common variants: {"tool":"search","parameters":{...}}
            if payload.get("tool") and payload.get("parameters") is not None:
                return [{"name": payload.get("tool"), "arguments": payload.get("parameters")}]
            # Common variants: {"tool_name":"search","tool_input":{...}}
            if payload.get("tool_name") and payload.get("tool_input") is not None:
                return [{"name": payload.get("tool_name"), "arguments": payload.get("tool_input")}]
            # Common variants: {"search": {...}} / {"visit": {...}}
            if len(payload) == 1:
                k = next(iter(payload.keys()))
                if k in TOOL_CLASS_MAP:
                    return [{"name": k, "arguments": payload[k]}]
            # Single call object.
            if payload.get("function") or payload.get("name") or payload.get("tool"):
                return [payload]
            return []
        if isinstance(payload, list):
            new_payload = []
            for call in payload:
                fn, call_args = extract_call(call)
                if fn and call_args:
                    new_payload.append({"name": fn, "arguments": call_args})
            return new_payload
        return []

    text = text.strip()
    raw_tools = re.findall(r"<tool_call>\s*(.*?)\s*</tool_call>", text, re.DOTALL)
    if raw_tools:
        try:
            tool_calls = [json.loads(k) for k in raw_tools]
            return _normalize_calls(tool_calls)
        except:
            try:
                tool_calls = [json_repair.loads(k) for k in raw_tools]
                tool_calls = [k for k in tool_calls if isinstance(k, dict)]
                if not tool_calls:
                    # 回退到匹配{}
                    raw_tools = re.findall(r"{\s*(.*?)\s*}", text, re.DOTALL)
                    tool_calls = [json_repair.loads(k) for k in raw_tools]
                    tool_calls = [k for k in tool_calls if isinstance(k, dict)]
                return _normalize_calls(tool_calls)
            except:
                return []

    m = re.search(r"<tool_calls>\s*(.*?)\s*</tool_calls>", text, re.S)
    if m:
        raw = m.group(1).strip()
        try:
            return _normalize_calls(json.loads(raw))
        except Exception:
            return []

    return []


def has_partial_tool_call(text: str) -> bool:
    return "<tool_call>" in text and "</tool_call>" not in text


def extract_call(call: Dict[str, Any]) -> Tuple[Optional[str], Dict[str, Any]]:
    function_obj = call.get("function")
    if isinstance(function_obj, dict):
        fn = function_obj.get("name") or call.get("name")
    elif isinstance(function_obj, str):
        # 兼容性: {"function":"search","parameters":{...}}
        fn = function_obj or call.get("name")
    else:
        fn = call.get("name")
    args = (
        (function_obj if isinstance(function_obj, dict) else {}).get("arguments")
        or (function_obj if isinstance(function_obj, dict) else {}).get("parameters")
        or call.get("arguments")
        or call.get("parameters")
        or call.get("input")
        or {}
    )
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            args = {"raw_arguments": args}
    if not isinstance(args, dict):
        args = {"raw_arguments": str(args)}
    return fn, args


def canonical_tool_call_signature(tool_calls: List[Dict[str, Any]]) -> str:
    normalized: List[Dict[str, Any]] = []
    for call in tool_calls:
        fn, args = extract_call(call)
        normalized.append({"name": fn or "", "arguments": args or {}})
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True)


def is_lnglat(text: str) -> bool:
    return bool(re.fullmatch(r"\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?\s*", text or ""))


def extract_first_lnglat(text: str) -> Optional[str]:
    m = re.search(r"-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?", text or "")
    if not m:
        return None
    return m.group(0).replace(" ", "")


async def retry_route_with_poi_if_needed(args, call_args: Dict[str, Any]) -> Optional[str]:
    origin = str(call_args.get("origin", ""))
    destination = str(call_args.get("destination", ""))
    if not origin or not destination:
        return None
    if is_lnglat(origin) and is_lnglat(destination):
        return None

    poi_tool = load_tool(args.tools_dir, "poi_search")
    route_tool = load_tool(args.tools_dir, "route_planning")
    if poi_tool is None or route_tool is None:
        return None

    try:
        origin_poi, dest_poi = await asyncio.gather(
            poi_tool.call({"address": origin}),
            poi_tool.call({"address": destination}),
        )
        origin_lnglat = extract_first_lnglat(str(origin_poi))
        destination_lnglat = extract_first_lnglat(str(dest_poi))
        if not origin_lnglat or not destination_lnglat:
            return None

        retry_args = dict(call_args)
        retry_args["origin"] = origin_lnglat
        retry_args["destination"] = destination_lnglat
        return await route_tool.call(retry_args)
    except Exception:
        return None


def has_final_answer(text: str) -> bool:
    return bool(re.search(r"<\s*answer\s*>.*?<\s*/\s*answer\s*>", text, re.I | re.S))


def has_answer_start(text: str) -> bool:
    return bool(re.search(r"<\s*answer\s*>", text, re.I))


def extract_answer_text(text: str) -> str:
    m = re.search(r"<\s*answer\s*>(.*?)<\s*/\s*answer\s*>", text or "", re.I | re.S)
    if not m:
        return ""
    return m.group(1).strip()


def infer_once(model, tokenizer, messages: List[Dict[str, str]], args) -> str:
    prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    enc = tokenizer(prompt, return_tensors="pt")
    enc = {k: v.to(model.device) for k, v in enc.items()}
    with torch.no_grad():
        out = model.generate(
            **enc,
            max_new_tokens=args.max_new_tokens,
            do_sample=args.do_sample,
            temperature=args.temperature,
            top_p=args.top_p,
            top_k=args.top_k,
        )
    return tokenizer.decode(out[0][enc["input_ids"].shape[-1]:], skip_special_tokens=True)


def infer_once_vllm(llm, tokenizer, messages: List[Dict[str, str]], args) -> str:
    from vllm import SamplingParams

    prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    sampling_params = SamplingParams(
        temperature=args.temperature,
        top_p=args.top_p,
        top_k=args.top_k,
        max_tokens=args.max_new_tokens,
    )
    outputs = llm.generate([prompt], sampling_params=sampling_params, use_tqdm=False)
    return outputs[0].outputs[0].text


def init_backend(args):
    tokenizer = AutoTokenizer.from_pretrained(args.model_dir, trust_remote_code=True)
    model = None
    llm = None
    if args.infer_backend == "transformers":
        model = AutoModelForCausalLM.from_pretrained(
            args.model_dir,
            torch_dtype=torch.bfloat16,
            device_map="auto",
            trust_remote_code=True,
        )
    elif args.infer_backend == "vllm":
        from vllm import LLM

        llm = LLM(
            model=args.model_dir,
            tokenizer=args.model_dir,
            trust_remote_code=True,
            tensor_parallel_size=args.vllm_tensor_parallel_size,
            gpu_memory_utilization=args.vllm_gpu_memory_utilization,
            max_model_len=args.vllm_max_model_len,
            dtype="bfloat16",
            disable_log_stats=True,
        )
    else:
        raise ValueError(f"Unsupported infer_backend: {args.infer_backend}")
    return tokenizer, model, llm


# ---------- 可复用的推理与工具执行 ----------

def infer(model_or_llm, tokenizer, messages, args):
    """统一推理入口 — transformers / vllm 自动分发"""
    if args.infer_backend == "vllm":
        return infer_once_vllm(model_or_llm, tokenizer, messages, args)
    else:
        return infer_once(model_or_llm, tokenizer, messages, args)


async def execute_tool(tools_dir: str, fn: str, call_args: Dict[str, Any],
                       tool_call_id: str, args) -> str:
    """执行单个工具调用并返回格式化后的结果字符串"""
    if not fn:
        return "TOOL_ERROR: missing function name"

    load_error = None
    try:
        tool = load_tool(tools_dir, fn)
    except Exception as e:
        tool = None
        load_error = e

    if load_error is not None:
        return f"TOOL_ERROR: failed to load tool `{fn}`: {type(load_error).__name__}: {load_error}"
    if tool is None:
        return f"TOOL_ERROR: unsupported tool `{fn}`"

    try:
        tool_result = await tool.call(call_args)
    except Exception as e:
        tool_result = f"TOOL_ERROR: {type(e).__name__}: {e}"

    # 路线规划自动回退：地名非经纬度时先 POI 查询再重试
    if (fn == "route_planning"
            and isinstance(tool_result, str)
            and "INVALID_PARAMS" in tool_result):
        retried = await retry_route_with_poi_if_needed(args, call_args)
        if retried:
            tool_result = retried

    tool_text = str(tool_result)
    max_chars = getattr(args, 'tool_response_max_chars', 5000)
    if len(tool_text) > max_chars:
        tool_text = (tool_text[:max_chars]
                     + f"\n...[truncated {len(tool_text) - max_chars} chars]")

    return tool_text


async def tool_loop(
    messages: List[Dict[str, Any]],
    args,
    tokenizer,
    model_or_llm,
    tools_dir: str,
) -> Dict[str, Any]:
    """核心 tool loop — run_single 和 interactive_chat 共用。

    参数:
        messages: 已构建好的初始消息列表（含 system + user），会就地修改
        args: 配置对象，需包含 max_turns / tool_first_enforce / tool_response_max_chars /
              print_chars / max_no_tool_no_answer_retries / max_invalid_tool_rounds /
              max_same_tool_call_rounds / force_answer_after_turns / tools_dir /
              infer_backend / temperature / top_p / top_k / max_new_tokens / do_sample
        tokenizer: tokenizer 实例
        model_or_llm: transformers 模型 或 vllm LLM 实例
        tools_dir: 工具目录路径

    返回:
        {"final_answer": str, "final_response": str, "status": str,
         "total_tool_calls": int, "turns": int}
    """
    quiet = getattr(args, 'quiet', True)

    no_tool_no_answer_retries = 0
    total_tool_calls = 0
    final_response = ""
    final_answer = ""
    status = "max_turns"

    saw_tool_response = False
    consecutive_invalid_tool_rounds = 0
    previous_tool_call_signature = ""
    previous_tool_response_signature = ""
    consecutive_same_tool_call_rounds = 0
    repeated_loop_final_answer_chance_used = False

    for turn in range(1, args.max_turns + 1):
        resp = infer(model_or_llm, tokenizer, messages, args)
        final_response = resp
        if not quiet:
            print(f"\n===== ROUND {turn}: ASSISTANT =====")
            print(resp[: getattr(args, 'print_chars', 1800)])

        messages.append({"role": "assistant", "content": resp})

        if (has_final_answer(resp) or has_answer_start(resp)) \
                and args.tool_first_enforce \
                and not saw_tool_response:
            messages.append({
                "role": "user",
                "content": "请先通过 tool_call 调用至少一个工具，并在收到工具结果后再输出最终<answer>。",
            })
            if not quiet:
                print("\n[continue] tool-first enforced: answer before tool_response is rejected")
            continue

        if (has_final_answer(resp) or has_answer_start(resp)) \
                and (not args.tool_first_enforce or saw_tool_response):
            final_answer = extract_answer_text(resp)
            status = "answer"
            if not quiet:
                print("\n[done] detected <answer> output")
            break

        tool_calls = parse_tool_calls(resp)
        if not tool_calls and has_partial_tool_call(resp):
            messages.append({
                "role": "user",
                "content": "你上一轮的工具调用不完整。请仅输出完整、可解析的结构化<tool_call>...</tool_call>。",
            })
            if not quiet:
                print("\n[continue] detected partial <tool_call>, ask model to complete it")
            continue

        if not tool_calls:
            max_retries = getattr(args, 'max_no_tool_no_answer_retries', 2)
            if no_tool_no_answer_retries < max_retries:
                no_tool_no_answer_retries += 1
                tool_first = getattr(args, 'tool_first_enforce', True)
                messages.append({
                    "role": "user",
                    "content": (
                        "请优先调用最合适的工具来获取事实信息，"
                        "并仅输出结构化 tool_call内容。用<tool_call>...</tool_call>包裹，正确格式示例："
                        '<tool_call>'
                        '{"name": "search", "arguments": {"query":["杭州西湖门票价格"]}}'
                        '</tool_call>'
                        "请严格输出合法JSON"
                        if tool_first
                        else "请不要继续思考或调用工具，直接给最终答案，并严格使用<answer>...</answer>格式。"
                    ),
                })
                if not quiet:
                    if tool_first:
                        print("\n[continue] no tool_call and no final answer, ask model to issue tool_call")
                    else:
                        print("\n[continue] no tool_call and no final answer, ask model for direct <Answer>")
                continue
            status = "no_tool_no_answer"
            if not quiet:
                print("\n[stop] no tool_call and no final answer")
            break
        no_tool_no_answer_retries = 0

        valid_tool_calls_in_round = 0
        round_tool_outputs: List[Dict[str, Any]] = []
        for i, call in enumerate(tool_calls, start=1):
            fn, call_args = extract_call(call)
            tool_call_id = call.get("id") if isinstance(call, dict) else None
            if not tool_call_id:
                tool_call_id = f"call_{turn}_{i}"
            total_tool_calls += 1
            if not quiet:
                print(f"\n----- TOOL {i}: {fn} args={call_args} -----")

            if not fn:
                tool_result = "TOOL_ERROR: missing function name"
            else:
                valid_tool_calls_in_round += 1
                tool_result = await execute_tool(tools_dir, fn, call_args, tool_call_id, args)

            tool_text = str(tool_result)
            if not quiet:
                print(tool_text[: getattr(args, 'print_chars', 1800)])

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": tool_text,
            })
            round_tool_outputs.append({
                "name": fn or "",
                "arguments": call_args or {},
                "result": tool_text,
            })
            saw_tool_response = True

        if valid_tool_calls_in_round == 0:
            consecutive_invalid_tool_rounds += 1
            max_invalid = getattr(args, 'max_invalid_tool_rounds', 3)
            if not quiet:
                print(f"\n[continue] invalid tool_call round {consecutive_invalid_tool_rounds}/{max_invalid}")
            if consecutive_invalid_tool_rounds >= max_invalid:
                status = "invalid_tool_call_loop"
                if not quiet:
                    print("\n[stop] too many consecutive invalid tool_call rounds, skip sample")
                break
            continue
        consecutive_invalid_tool_rounds = 0

        current_tool_call_signature = canonical_tool_call_signature(tool_calls)
        current_tool_response_signature = json.dumps(
            round_tool_outputs, ensure_ascii=False, sort_keys=True
        )
        if (current_tool_call_signature == previous_tool_call_signature
                and current_tool_response_signature == previous_tool_response_signature):
            consecutive_same_tool_call_rounds += 1
            max_same = getattr(args, 'max_same_tool_call_rounds', 3)
            if consecutive_same_tool_call_rounds >= max_same:
                if not repeated_loop_final_answer_chance_used:
                    repeated_loop_final_answer_chance_used = True
                    messages.append({
                        "role": "user",
                        "content": (
                            "检测到你连续重复了相同工具调用与相同结果。不要重复循环。"
                            "现在给你最后一次机会：禁止继续调用工具，"
                            "请直接输出最终结果，并严格使用<answer>...</answer>。"
                        ),
                    })
                    if not quiet:
                        print("\n[continue] repeated tool_call+result loop detected, inject final forced <answer> chance")
                    continue
                status = "repeated_tool_call_loop"
                if not quiet:
                    print("\n[stop] repeated identical tool_call+result rounds reached limit, skip sample")
                break
        else:
            consecutive_same_tool_call_rounds = 0
            previous_tool_call_signature = current_tool_call_signature
            previous_tool_response_signature = current_tool_response_signature

        force_after = getattr(args, 'force_answer_after_turns', 12)
        if turn >= force_after:
            messages.append({
                "role": "user",
                "content": "信息已经足够。禁止继续调用工具，请直接给最终方案，并严格用<answer>...</answer>输出。",
            })
    else:
        status = "max_turns"
        if not quiet:
            print("\n[stop] max_turns reached without final answer")

    return {
        "final_answer": final_answer,
        "final_response": final_response,
        "status": status,
        "total_tool_calls": total_tool_calls,
        "turns": min(turn, args.max_turns),
    }


async def run_single(args, tokenizer, model, llm, sample_idx: int) -> Dict[str, Any]:
    base_dir = str(Path(args.tools_dir).resolve().parent)
    if base_dir not in sys.path:
        sys.path.insert(0, base_dir)

    row = load_jsonl_row(args.dataset_path, sample_idx)
    conversations = row.get("conversations") or row.get("messages") or row.get("answer")
    if not conversations or len(conversations) < 2:
        return {
            "sample_idx": sample_idx,
            "id": row.get("id"),
            "status": "invalid_sample",
            "error": "need at least system+user",
        }

    messages = conversations[:2]
    if args.override_system_prompt_file:
        system_prompt = load_system_prompt_from_dataset(
            args.override_system_prompt_file, args.override_system_prompt_idx
        )
        messages[0] = {"role": "system", "content": system_prompt}
    if messages:
        normalize_system_prompt_inplace(messages, args.system_max_tool_calls)
    if not args.quiet:
        print(f"[sample_idx={sample_idx}] user: {messages[1]['content'][:200]}")

    model_or_llm = llm if args.infer_backend == "vllm" else model
    result = await tool_loop(messages, args, tokenizer, model_or_llm, args.tools_dir)

    return {
        "sample_idx": sample_idx,
        "id": row.get("id"),
        "query": messages[1]["content"],
        "status": result["status"],
        "turns": result["turns"],
        "tool_calls": result["total_tool_calls"],
        "prediction": result["final_answer"] or result["final_response"],
        "has_answer_tag": bool(result["final_answer"]),
        **(
            {"messages": messages}
            if getattr(args, "save_full_messages", False)
            else {}
        ),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--infer_backend", default="transformers", choices=["transformers", "vllm"])
    parser.add_argument("--model_dir", required=True)
    parser.add_argument("--dataset_path", required=True)
    parser.add_argument("--tools_dir", default="tools")
    parser.add_argument("--sample_idx", type=int, default=0)
    parser.add_argument("--max_turns", type=int, default=20)
    parser.add_argument(
        "--system_max_tool_calls",
        type=int,
        default=8,
        help="Rewrite system prompt '最大可调用N轮工具' to this value; <=0 disables rewrite.",
    )
    parser.add_argument("--max_new_tokens", type=int, default=8192)
    parser.add_argument("--do_sample", action="store_true", default=True)
    parser.add_argument("--temperature", type=float, default=0.6)
    parser.add_argument("--top_p", type=float, default=0.95)
    parser.add_argument("--top_k", type=int, default=50)
    parser.add_argument("--print_chars", type=int, default=1800)
    parser.add_argument("--tool_response_max_chars", type=int, default=5000)
    parser.add_argument("--force_answer_after_turns", type=int, default=12)
    parser.add_argument("--max_no_tool_no_answer_retries", type=int, default=2)
    parser.add_argument(
        "--max_invalid_tool_rounds",
        type=int,
        default=3,
        help="Skip current sample after this many consecutive rounds where all parsed tool_calls are invalid.",
    )
    parser.add_argument(
        "--max_same_tool_call_rounds",
        type=int,
        default=3,
        help="Skip sample when identical tool_calls are repeated for this many consecutive rounds (before a final forced-answer chance).",
    )
    parser.add_argument("--override_system_prompt_file", default="")
    parser.add_argument("--override_system_prompt_idx", type=int, default=0)
    parser.add_argument("--vllm_tensor_parallel_size", type=int, default=1)
    parser.add_argument("--vllm_gpu_memory_utilization", type=float, default=0.7)
    parser.add_argument("--vllm_max_model_len", type=int, default=32000)
    parser.add_argument("--start_idx", type=int, default=0)
    parser.add_argument("--num_samples", type=int, default=1)
    parser.add_argument("--output_path", default="")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--save_full_messages",
        action="store_true",
        help="Save full conversation trajectory (messages) to output JSONL.",
    )
    parser.add_argument(
        "--tool_first_enforce",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Require at least one tool_response before accepting final answer.",
    )
    args = parser.parse_args()
    if args.max_turns <= 1:
        args.force_answer_after_turns = 1
    elif args.force_answer_after_turns >= args.max_turns:
        args.force_answer_after_turns = args.max_turns - 1
        if not args.quiet:
            print(
                f"[adjust] force_answer_after_turns clamped to max_turns-1: {args.force_answer_after_turns}"
            )

    tokenizer, model, llm = init_backend(args)

    start_idx = args.start_idx
    end_idx = start_idx + args.num_samples

    if args.output_path:
        Path(args.output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output_path).write_text("", encoding="utf-8")

    for i in range(start_idx, end_idx):
        try:
            result = asyncio.run(run_single(args, tokenizer, model, llm, i))
        except:
            print(f"sample_idx={i} inference failed, skipped.")
            continue
        if args.output_path:
            with open(args.output_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(result, ensure_ascii=False) + "\n")
        if not args.quiet:
            print(f"[sample_idx={i}] status={result.get('status')} tool_calls={result.get('tool_calls')}")

