# -*- coding: utf-8 -*-
# --------------------------------------------
# 文件描述: 模型效果综合评估
# --------------------------------------------

import argparse
import asyncio
import json
import os
import re
import time
from statistics import mean

import numpy as np
from openai import AsyncOpenAI
from dotenv import load_dotenv

from prompts.prompt import llm_judge_system_prompt 

# 加载环境变量
load_dotenv()

_API_KEY = os.environ.get("OPENAI_API_KEY", "")
_BASE_URL = os.environ.get("OPENAI_BASE_URL", "")
_MODEL = "gpt-5.4-mini"


class LLMJudge:
    def __init__(self, llm=None, model=None):
        base_url = os.environ.get("OPENAI_BASE_URL") or _BASE_URL
        api_key = os.environ.get("OPENAI_API_KEY") or _API_KEY
        self.model = model or os.environ.get("JUDGE_MODEL_ID") or _MODEL
        if not llm:
            if not api_key or not base_url:
                raise RuntimeError("缺少 OPENAI_API_KEY / OPENAI_BASE_URL，无法调用裁判模型")
            self.llm = AsyncOpenAI(
                base_url=base_url,
                api_key=api_key,
                timeout=300
            )
        else:
            self.llm = llm

        self.system_prompt = llm_judge_system_prompt

        self.score_a_pattern = re.compile(
            r'"combined_scores"\s*:\s*\{[^{}]*?"Agent_A"\s*:\s*([0-9]+(?:\.[0-9]+)?)',
            re.S | re.I,
        )
        self.score_b_pattern = re.compile(
            r'"combined_scores"\s*:\s*\{[^{}]*?"Agent_B"\s*:\s*([0-9]+(?:\.[0-9]+)?)',
            re.S | re.I,
        )
        self.winner_pattern = re.compile(
            r'"winner"\s*:\s*"(?P<winner>Agent_A|Agent_B|Tie)"', re.I
        )

    async def _compute_unidirectional(
        self, prediction: list[dict], reference: list[dict], query: str
    ) -> tuple[float, float]:
        trajectory_a, answer_a = self.process_messages(prediction)
        trajectory_b, answer_b = self.process_messages(reference)
        # print("trajectory_a", trajectory_a)
        # print("answer_a", answer_a)
        # print("trajectory_b", trajectory_b)
        # print("answer_b", answer_b)

        prompt = f"""<USER_QUERY>\n{query}\n</USER_QUERY>\n\n<PATH_A>\n{trajectory_a}\n</PATH_A>\n\n<PATH_B>\n{trajectory_b}\n</PATH_B>\n\n<Answer_A>\n{answer_a}\n</Answer_A>\n\n<Answer_B>\n{answer_b}\n</Answer_B>"""
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": prompt},
        ]

        response = await self.llm.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0.0,
            top_p=1.0,
            seed=0,
        )
        score_a, score_b = self.get_judge_scores(response.choices[0].message.content)

        return score_a, score_b

    async def compute(
        self, prediction: list[dict], reference: list[dict], query: str
    ) -> dict[str, float]:
        # 双向打分，消除位置的bias
        results = await asyncio.gather(
            self._compute_unidirectional(prediction, reference, query=query),
            self._compute_unidirectional(reference, prediction, query=query),
        )

        # 这里要注意顺序
        score_prediction = (results[0][0] + results[1][1]) / 2
        score_reference = (results[0][1] + results[1][0] ) / 2

        return {"query:": query, "prediction": round(score_prediction, 2), "reference": round(score_reference, 2)}

    def process_messages(self, messages: list[dict]) -> tuple[list[dict], str]:
        step_idx = 0
        trajectory = []
        for message in messages[:-1]:
            if message["role"] != "assistant":
                continue

            if "tool_calls" in message:
                trajectory.append(
                    {
                        "step": step_idx,
                        "tool_calls": message.get("tool_calls", ""),
                    }
                )
            else:
                content = re.sub(r'<think>.*?</think>', '', message["content"], flags=re.DOTALL).strip()
                if content:
                    trajectory.append(
                        {
                            "step": step_idx,
                            "tool_calls": content,
                        }
                    )
            step_idx += 1

        answer = "未回复"
        if messages[-1]["role"] == "assistant":
            answer = messages[-1].get("content") or answer
            answer = re.sub(r'<think>.*?</think>', '', answer, flags=re.DOTALL).strip()

        return trajectory, answer

    def get_judge_scores(self, response: str) -> tuple[float, float]:
        match_a = self.score_a_pattern.search(response)
        match_b = self.score_b_pattern.search(response)

        if not (match_a and match_b):
            raise ValueError(f"Failed to get judge scores in response: {response}")

        score_a = float(match_a.group(1))
        score_b = float(match_b.group(1))

        return score_a, score_b


    def chunk_by_size(self, lst, chunk_size):
        """将列表按固定大小拆分"""
        return [lst[i:i + chunk_size] for i in range(0, len(lst), chunk_size)]


    async def process_batch(self, prediction, reference, query, chunk_task_size=20):
        """并发处理所有数据"""
        tasks = [self.compute(p, r, q) for p, r, q in zip(prediction, reference, query)]
        split_tasks = self.chunk_by_size(tasks, chunk_task_size)
        final_results = []
        for idx, single_task in enumerate(split_tasks):
            results = await asyncio.wait_for(
                asyncio.gather(*single_task),
                timeout=300
            )
            final_results.extend(results)
            print(f"task_{idx} finished, processed {len(results)} samples.")
        return final_results


def paired_bootstrap_ci(diffs, n_boot=20000, seed=0, alpha=0.05):
    """对逐题差值做配对 Bootstrap，返回 (均值, 下界, 上界)。"""
    arr = np.asarray(diffs, dtype=float)
    if arr.size == 0:
        return 0.0, 0.0, 0.0
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, arr.size, size=(n_boot, arr.size))
    means = arr[idx].mean(axis=1)
    lo, hi = np.percentile(means, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return float(arr.mean()), float(lo), float(hi)


def load_records(path):
    table = {}
    with open(path, encoding="utf-8") as fd:
        for line in fd:
            line = line.strip()
            if not line:
                continue
            info = json.loads(line)
            table[info["query"]] = info
    return table


def build_pairs(reference_path, prediction_path, limit=0):
    base = load_records(reference_path)
    pred = load_records(prediction_path)
    predictions, references, queries = [], [], []
    for key, value in pred.items():
        if key not in base:
            continue
        queries.append(base[key]["query"])
        references.append(base[key]["messages"])
        predictions.append(value["messages"])
    if limit > 0:
        queries, references, predictions = queries[:limit], references[:limit], predictions[:limit]
    return predictions, references, queries


def main():
    parser = argparse.ArgumentParser(description="双向配对 LLM Judge 评测")
    parser.add_argument("--reference", required=True, help="对照组模型输出 jsonl")
    parser.add_argument("--prediction", required=True, help="实验组模型输出 jsonl")
    parser.add_argument("--output_dir", default=None, help="结果输出目录，省略则只打印")
    parser.add_argument("--tag", default=None, help="结果文件前缀，默认取两个输入文件名组合")
    parser.add_argument("--model", default=None, help="裁判模型，默认取 JUDGE_MODEL_ID 或内置值")
    parser.add_argument("--limit", type=int, default=0, help="只评测前 N 条，0 表示全部")
    parser.add_argument("--chunk_size", type=int, default=20, help="并发批次大小")
    parser.add_argument("--bootstrap", type=int, default=20000, help="Bootstrap 重采样次数")
    args = parser.parse_args()

    predictions, references, queries = build_pairs(args.reference, args.prediction, args.limit)
    if not predictions:
        raise SystemExit("两组输出没有可配对的 query，请检查输入文件")

    print(f"配对数: {len(predictions)}  裁判模型: {args.model or os.environ.get('JUDGE_MODEL_ID') or _MODEL}")
    judge = LLMJudge(model=args.model)
    results = asyncio.run(judge.process_batch(predictions, references, queries, chunk_task_size=args.chunk_size))

    diffs = [r["prediction"] - r["reference"] for r in results]
    mean_diff, lo, hi = paired_bootstrap_ci(diffs, n_boot=args.bootstrap)
    wins = sum(1 for d in diffs if d > 0)
    ties = sum(1 for d in diffs if d == 0)
    losses = sum(1 for d in diffs if d < 0)

    summary = {
        "n": len(results),
        "reference_mean": round(mean(r["reference"] for r in results), 4),
        "prediction_mean": round(mean(r["prediction"] for r in results), 4),
        "delta": round(mean_diff, 4),
        "ci95": [round(lo, 4), round(hi, 4)],
        "wins": wins,
        "ties": ties,
        "losses": losses,
        "bootstrap_rounds": args.bootstrap,
        "judge_model": args.model or os.environ.get("JUDGE_MODEL_ID") or _MODEL,
        "reference_file": args.reference,
        "prediction_file": args.prediction,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if args.output_dir:
        tag = args.tag or f"{pathlib.Path(args.reference).stem}__vs__{pathlib.Path(args.prediction).stem}"
        out_dir = pathlib.Path(args.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        with open(out_dir / f"{tag}.jsonl", "w", encoding="utf-8") as fd:
            for r in results:
                fd.write(json.dumps(r, ensure_ascii=False) + "\n")
        with open(out_dir / f"{tag}_summary.json", "w", encoding="utf-8") as fd:
            json.dump(summary, fd, ensure_ascii=False, indent=2)
        print(f"已写入 {out_dir}")


if __name__ == "__main__":
    import pathlib

    main()
