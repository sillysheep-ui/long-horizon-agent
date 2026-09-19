# -*- coding: utf-8 -*-
# --------------------------------------------
# 文件描述: Tavily网页抓取工具 
# --------------------------------------------


import asyncio
import random
import itertools
import copy
import json
import os
import time
import requests
import aiohttp
import tiktoken
import json_repair

from dotenv import load_dotenv
from openai import AsyncOpenAI
from qwen_agent.tools.base import BaseTool, register_tool

from utils import logging as agent_logging
from prompts.prompt import EXTRACTOR_PROMPT
from tavily import AsyncTavilyClient


# 加载环境变量
load_dotenv()


# 初始化logger
logger = agent_logging.get_logger()


TAVILY_API_KEY = os.environ["TAVILY_API_KEY"].split(",")
VISIT_SERVER_MAX_RETRIES = int(os.getenv("VISIT_SERVER_MAX_RETRIES", 1))
VISIT_SERVER_TIMEOUT = int(os.getenv("VISIT_SERVER_TIMEOUT", 180))
WEBCONTENT_MAXLENGTH = int(os.getenv("WEBCONTENT_MAXLENGTH", 100000))


def truncate_to_tokens(text: str, max_tokens: int = 100000) -> str:
    encoding = tiktoken.get_encoding("cl100k_base")

    tokens = encoding.encode(text)
    if len(tokens) <= max_tokens:
        return text

    truncated_tokens = tokens[:max_tokens]
    return encoding.decode(truncated_tokens)


@register_tool("visit", allow_overwrite=True)
class Visit(BaseTool):
    name = "visit"
    description = "访问网页并根据目标信息返回内容摘要。"
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": ["string", "array"],
                "items": {"type": "string"},
                "minItems": 1,
                "description": "要访问的网页URL，可为单个URL或URL数组。",
            },
            "goal": {
                "type": "string",
                "description": "访问网页需要获得的目标信息。",
            },
        },
        "required": ["url", "goal"],
    }

    # The `call` method is the main function of the tool.
    def __init__(self, cfg: dict | None = None, summary_client=None):
        super().__init__(cfg)

        if not summary_client:
            self._llm_client = AsyncOpenAI(
                base_url=os.environ.get("OPENAI_BASE_URL"),
                api_key=os.environ.get("OPENAI_API_KEY")
            )
        else:
            self._llm_client = summary_client 
        self._tavily_clients = [AsyncTavilyClient(api_key=api_key) for api_key in TAVILY_API_KEY]


    async def call(self, params: str | dict, **kwargs) -> str:  # type: ignore[override]
        try:
            url = params["url"]
            goal = params["goal"]
        except Exception:
            return "[Visit] Invalid request format: Input must be a JSON object containing 'url' and 'goal' fields"

        start_time = time.time()

        if isinstance(url, str):
            logger.debug(f"Visiting single URL: {url}")
            response = await self.readpage(url, goal)
        else:
            response = []
            logger.debug(f"Visiting multiple URLs: {url}")
            assert isinstance(url, list)
            start_time = time.time()

            # 并行调用 readpage
            tasks = [self.readpage(u, goal) for u in url]
            results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=VISIT_SERVER_TIMEOUT)

            for u, result in zip(url, results):
                if isinstance(result, Exception):
                    cur_response = f"Error fetching content {u}: {result}"
                else:
                    cur_response = result
                response.append(cur_response)

            response = "\n=======\n".join(response)

        logger.debug(f"Summary Length {len(response)}; Summary Content {response}")
        return response.strip()

    async def call_server(self, msgs, max_retries=1):
        summary_args = dict(
            model=os.environ.get("LLM_MODEL_ID"),
            messages=msgs
        )
        for attempt in range(max_retries):
            try:
                chat_response = await self._llm_client.chat.completions.create(**summary_args)
                content = chat_response.choices[0].message.content
                if content:
                    try:
                        json.loads(content)
                    except Exception:
                        left = content.find("{")
                        right = content.rfind("}")
                        if left != -1 and right != -1 and left <= right:
                            content = content[left : right + 1]
                    return content
            except Exception:
                if attempt == (max_retries - 1):
                    return ""
                await asyncio.sleep(0.5)
                continue

    async def html_readpage(self, url: str, max_attempts: int=2) -> str:
        for attempt in range(max_attempts):
            logger.debug(
                f"html_readpage {url} attempt {attempt + 1}/{max_attempts}"
            )
            client = random.choice(self._tavily_clients)
            async with aiohttp.ClientSession() as session:
                try:
                    response = await client.extract(urls=[url])
                    content = response["results"][0]['raw_content']
                    print(111, client)
                except Exception as e:
                    logger.debug(f"html_readpage {url} failed with error {e}")
                    content = "[visit] Failed to read page."
                    await asyncio.sleep(1)
            if (
                content
                and not content.startswith("[visit] Failed to read page.")
            ):
                return content
        return "[visit] Failed to read page."

    async def readpage(self, url: str, goal: str) -> str:
        """Read and summarize a webpage using Jina + LLM extractor."""
        summary_page_func = self.call_server
        max_retries = VISIT_SERVER_MAX_RETRIES
        _content = await self.html_readpage(url)
        content = copy.deepcopy(_content)
        valid_content = (
            content
            and not content.startswith("[visit] Failed to read page.")
            and content != "[visit] Empty content."
            and not content.startswith("[document_parser]")
        )
        logger.debug(
            f"readpage content: {len(content)}, valid_content: {valid_content}"
        )
        if not valid_content:
            useful_information = (
                f"The useful information in {url} for user goal {goal} as follows: \n\n"
            )
            useful_information += (
                "Evidence in page: \n"
                + "The provided webpage content could not be accessed. Please check the URL or file format."
                + "\n\n"
            )
            useful_information += (
                "Summary: \n"
                + "The webpage content could not be processed, and therefore, no information is available."
                + "\n\n"
            )
            return useful_information

        return content[:10]
        content = truncate_to_tokens(content, max_tokens=WEBCONTENT_MAXLENGTH)
        messages = [
            {
                "role": "user",
                "content": EXTRACTOR_PROMPT.format(webpage_content=content, goal=goal),
            }
        ]
        raw = await summary_page_func(messages, max_retries=max_retries)
        summary_retries = 1
        while isinstance(raw, str) and len(raw) < 10 and summary_retries >= 0:
            truncate_length = int(0.7 * len(content)) if summary_retries > 0 else 25000
            status_msg = (
                f"[visit] Summary url[{url}] attempt {3 - summary_retries + 1}/3, content length: {len(content)}, truncating to {truncate_length} chars"
                if summary_retries > 0
                else f"[visit] Summary url[{url}] failed after 3 attempts, final truncation to 25000 chars"
            )
            logger.debug(status_msg)
            content = content[:truncate_length]
            extraction_prompt = EXTRACTOR_PROMPT.format(
                webpage_content=content, goal=goal
            )
            messages = [{"role": "user", "content": extraction_prompt}]
            raw = await summary_page_func(messages, max_retries=max_retries)
            summary_retries -= 1

        if isinstance(raw, str):
            raw = raw.replace("```json", "").replace("```", "").strip()

        logger.debug("Final raw response:", len(raw))

        parse_retry_times = 0

        try:
            raw_obj = json.loads(raw)
        except Exception:
            try:
                raw_obj = json_repair.loads(raw)
            except Exception:
                raw_obj = None

        if raw_obj is None:
            useful_information = f"The provided webpage content: {_content[:1000]}"
            return useful_information

        useful_information = (
            f"The useful information in {url} for user goal {goal} as follows: \n\n"
        )
        useful_information += (
            "Evidence in page: \n" + str(raw_obj.get("evidence", "")) + "\n\n"
        )
        useful_information += "Summary: \n" + str(raw_obj.get("summary", "")) + "\n\n"

        if len(useful_information) < 10 and summary_retries < 0:
            logger.debug(
                "[visit] Could not generate valid summary after maximum retries"
            )
            useful_information = "[visit] Failed to read page"
        logger.debug(f"Final useful_information: {useful_information}")
        return useful_information


if __name__ == "__main__":
    search = Visit()
    params =  {"url": "https://baike.baidu.com/item/%E9%99%88%E6%99%AF%E6%B6%A6/18067", "goal": "陈景润 重要获奖 成就"}
    res = asyncio.run(search.call(params))
    print(res)
