# -*- coding: utf-8 -*-
# --------------------------------------------
# 文件描述: 蒸馏推理路径和答案
# --------------------------------------------

import json
import re
import os
import ast
import sys
import time
import json5
import hashlib
import asyncio
import datetime
import json_repair
from pathlib import Path
from dotenv import load_dotenv
from pydantic import BaseModel
from qwen_agent.agents.fncall_agent import FnCallAgent
from qwen_agent.llm.schema import Message

from transformers import AutoTokenizer
from transformers import PreTrainedTokenizerFast
from openai import AsyncOpenAI

from utils import logging as agent_logging
from tools.tool_web_search import WebSearch
from tools.tool_visit import Visit
from tools.tool_weather import WeatherSearch
from tools.tool_transport import FlightsSearch
from tools.tool_train_ticket import TrainTicketsSearch
from tools.tool_route_planning import RoutePlanning
from tools.tool_poi_search import POISearch
from tools.tool_around_search import AroundSearch
from prompts.prompt import AGENTIC_SYSTEM_PROMPT


# 初始化logger
logger = agent_logging.get_logger(__name__)

# 加载环境变量
load_dotenv()


def today_date():
    return datetime.date.today().strftime("%Y-%m-%d")


class MultiTurnReactAgent(FnCallAgent):
    def __init__(
        self,
        tokenizer: PreTrainedTokenizerFast | str = None,
        max_tokens_per_turn: int = 10000,
        max_llm_calls_per_run: int = 50,
        max_total_tokens: int = 204800,
    ):

        if not tokenizer:
            llm_model_path = "pretrained/Qwen/Qwen3-4B-Instruct-2507"
            self.tokenizer = AutoTokenizer.from_pretrained(llm_model_path)
        elif isinstance(tokenizer, str):
            self.tokenizer = AutoTokenizer.from_pretrained(tokenizer)
        else:
            self.tokenizer =  tokenizer

        self.max_tokens_per_turn = max_tokens_per_turn
        self.max_llm_calls_per_run = max_llm_calls_per_run
        self.max_total_tokens = max_total_tokens
        self.max_total_tokens_before_finishing = int(max_total_tokens * 0.8)
        self.tool_class = [
            Visit(),
            WebSearch(),
            WeatherSearch(),
            FlightsSearch(),
            TrainTicketsSearch(),
            RoutePlanning(),
            POISearch(),
            AroundSearch(),
        ]
        self.tool_map = {tool.name: tool for tool in self.tool_class}
        self.tools = []
        for tool_name in self.tool_map:
            tool = self.tool_map[tool_name]
            self.tools.append({
                "type": "function",
                "function": tool.function
            })

    def count_tokens(self, messages):
        message_strs = []
        for msg in messages:
            if isinstance(msg, BaseModel):
                msg = msg.model_dump()
                assert "role" in msg and "content" in msg
            message_strs.append(
                f"<|im_start|>{msg['role']}\n{msg['content']}<|im_end|>\n"
            )
        message_strs.append("<|im_start|>assistant\n")
        prompt_token_ids = self.tokenizer.encode("".join(message_strs))
        return len(prompt_token_ids)

    async def call_server(
        self, client: AsyncOpenAI, messages: list[dict], max_attempts: int = 2
    ) -> str:
        attempts = 0
        while attempts < max_attempts:
            try:
                completion = await client.chat.completions.create(
                    model=os.environ["AGENT_MODEL_ID"],
                    messages=messages,
                    tools=self.tools,
                    extra_body={
                        "reasoning_split": True,
                        "enable_thinking":True
                    },
                    max_completion_tokens=self.max_tokens_per_turn,
                )
                message = completion.choices[0].message
                assert message, "Error: LLM response is empty."
                return completion, message
            except RuntimeError as e:
                attempts+=1
                logger.warning(
                    f"RuntimeError during LLM call_server at attempt {attempts}: {e}"
                )
                continue
        raise RuntimeError(
            f"Failed to get response from LLM after {max_attempts} attempts."
        )

    async def run_agent(
        self, data, client: AsyncOpenAI, save_path: str | None = None
    ) -> list[list[Message]]:
        start_time = time.time()
        qid = data["qid"]
        question = data["question"]
        answer = data.get("answer", "")
        self.user_prompt = question
        system_prompt = AGENTIC_SYSTEM_PROMPT 
        cur_date = today_date()
        system_prompt = system_prompt.replace("__CURRENT_DATE__", str(cur_date))
        system_prompt = system_prompt.replace("__MAX_TOOL_CALL__", str(self.max_llm_calls_per_run))
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question},
        ]
        stats = dict(
            turns=0
        )
        num_llm_calls_available = self.max_llm_calls_per_run
        completions = []
        round = 0
        while num_llm_calls_available > 0:
            # Check whether time is reached
            if time.time() - start_time > 10 * 60:  # 10 minutes in seconds
                prediction = "No answer found after 10mins"
                termination = "No answer found after 10mins"
                result = {
                    "question": question,
                    "answer": answer,
                    "messages": messages,
                    "prediction": prediction,
                    "termination": termination,
                }
                return result

            round += 1
            stats["turns"] += 1
            num_llm_calls_available -= 1
            completion, message = await self.call_server(client, messages)
            content = message.content
            completions.append(completion)
            messages.append(message)

            # 打印response
            if content:
                logger.info(f"[ASSITANT] LLM response: {content}")

            if message.tool_calls:
                try:
                    tool_calls = [
                        {
                            "id": item.id,
                            "name": item.function.name,
                            "arguments": json.loads(item.function.arguments)
                        } for item in message.tool_calls
                    ]
                except Exception:
                    try:
                        tool_calls = [
                            {
                                "id": item.id,
                                "name": item.function.name,
                                "arguments": json_repair.loads(item.function.arguments)
                            } for item in message.tool_calls
                        ]
                    except:
                        tool_calls = []
                        logger.warn(f"Parse tool_calls {matches} failed")
                tasks = [self.custom_call_tool(tool_call) for tool_call in tool_calls]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                # 处理异常情况
                for tool_call, result in zip(tool_calls, results):
                    if isinstance(result, Exception):
                        result = "<tool_response>\n" + f"Fetch {tool_call} response failed." + "\n</tool_response>"

                    logger.info(f"[Tool] execute function call: {tool_call}")
                    messages.append({"role": "tool", "tool_call_id": tool_call["id"], "content": result})
                    print(tool_call, result)

                    tool_name = tool_call["name"]
                    if tool_name not in stats:
                        stats[tool_name] = 0
                    stats[tool_name] += 1

            if content and "<answer>" in content and "</answer>" in content:
                termination = "answer"
                break

            if num_llm_calls_available <= 0 and "<answer>" not in content:
                messages.append(
                    {
                        "role": "user",
                        "content": "Sorry, the number of llm calls exceeds the limit. You should stop making tool calls and, "
                        "based on all the information above, think again and provide what you consider the most likely answer "
                        "in the following format:<think>your final thinking</think>\n<answer>your answer</answer>",
                    }
                )

            max_tokens = self.max_total_tokens_before_finishing
            token_count = self.count_tokens(messages)
            logger.debug(
                f"QID {data['qid']} Round: {round}, token count: {token_count}"
            )

            if token_count > max_tokens:
                logger.debug(
                    f"QID {data['qid']} Token quantity exceeds the limit: {token_count} > {max_tokens}"
                )
                messages.append(
                    {
                        "role": "user",
                        "content": "You have now reached the maximum context length you can handle. "
                        "You should stop making tool calls and, based on all the information above, "
                        "think again and provide what you consider the most likely answer in the following format:"
                        "<think>your final thinking</think>\n<answer>your answer</answer>",
                    }
                )
                completion, message = await self.call_server(client, messages)
                completions.append(completion)
                content = message.content
                messages.append(message)
                if "<answer>" in content and "</answer>" in content:
                    prediction = content.split("<answer>")[1].split("</answer>")[0]
                    termination = "generate an answer as token limit reached"
                else:
                    prediction = content
                    termination = (
                        "format error: generate an answer as token limit reached"
                    )
                result = {
                    "question": question,
                    "answer": answer,
                    "messages": messages,
                    "prediction": prediction,
                    "termination": termination,
                    "completions": completions,
                    "stats": stats,
                }
                token_count = self.count_tokens(messages)
                if token_count > self.max_total_tokens:
                    logger.warning(
                        f"Warning: total token count {token_count} exceeds the hard limit {self.max_total_tokens}."
                    )
                return result

        if "<answer>" in content:
            prediction = content.split("<answer>")[1].split("</answer>")[0]
            termination = "answer"
        else:
            prediction = "No answer found."
            termination = "answer not found"
            if num_llm_calls_available == 0:
                termination = "exceed available llm calls"
        result = {
            "question": question,
            "answer": answer,
            "messages": [
                m.model_dump() if isinstance(m, BaseModel) else m for m in messages
            ],
            "prediction": prediction,
            "termination": termination,
            "completions": completions,  # final completion
            "stats": stats,
        }
        if save_path:
            to_dump = dict(**result)
            to_dump.pop("completions")
            save_path = f"{save_path}/{data['qid']}.json"
            with open(save_path, "w", encoding="utf-8") as f:
                json.dump(to_dump, f, ensure_ascii=False, indent=4)
            logger.debug(f"Result dumped to {save_path}")

        return result

    async def custom_call_tool(self, tool_call: dict, **kwargs):
        tool_name = tool_call["name"]
        tool_args = tool_call.get("arguments", {})
        if tool_name in self.tool_map:
            tool_args["params"] = tool_args
            raw_result = await self.tool_map[tool_name].call(tool_args, **kwargs)
            result = raw_result
            return result
        else:
            return f"Error: Tool {tool_name} not found"


    async def make_trajectory(
        self,
        data: dict[str, str] | list[dict],
        client: AsyncOpenAI,
        save_path: str | None = None 
    ) -> dict:
        if isinstance(data, dict):
            result = await self.run_agent(data, client, save_path=save_path)
        elif isinstance(data, list):
            for item in data:
                try:
                    result = await self.run_agent(item, client, save_path=save_path)
                except Exception:
                    logger.warning(f"rollout question: {item['question']} failed.")


async def arun_episode(data, save_path: str | None = None):
        agent = MultiTurnReactAgent()
        client = AsyncOpenAI(
            base_url=os.environ.get("AGENT_BASE_URL"),
            api_key=os.environ.get("AGENT_API_KEY")
        )
        # Collect single trajectory
        result = await agent.make_trajectory(
            data=data,
            client=client,
            save_path=save_path
        )
        return result

if __name__ == "__main__":
    #  1.unit-test （单样本测试）
    """
    data = {
        "qid": "123",
        "question": "去重庆旅游时，如何在预算有限的情况下规划一条包含洪崖洞和长江索道的经济路线？",
    }
    asyncio.run(arun_episode(data, "saved/tmp/"))

    """

    # 2. large scale call (生成训练数据)
    #for filename in ["train_expansion.jsonl", "test.jsonl"]
    for filename in ["rl_buchong_left_316.jsonl"]:
        start = time.time()
        data = []
        phase = filename.split(".")[0]
        save_path = f"saved/{phase}/"
        with open(f"data/{filename}") as fd:
            for line in fd:
                info = json.loads(line)
                question = info["question"]
                # info["qid"] = hashlib.md5(question.encode('utf-8')).hexdigest()
                data.append(info)
        asyncio.run(arun_episode(data, save_path))
        end = time.time()
        print(f"{filename} rollout cost: ", end - start)
