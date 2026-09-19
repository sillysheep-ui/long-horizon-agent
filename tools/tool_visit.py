# -*- coding: utf-8 -*-
# --------------------------------------------
# 文件描述: 网页抓取工具 
# --------------------------------------------


import asyncio
import copy
import json
import os
import time

import json_repair
import requests
import tiktoken
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from firecrawl import Firecrawl
from openai import OpenAI
from qwen_agent.tools.base import BaseTool, register_tool

from prompts.prompt import EXTRACTOR_PROMPT
from utils import logging as agent_logging


# 加载环境变量
load_dotenv()


logger = agent_logging.get_logger()


VISIT_SERVER_MAX_RETRIES = int(os.getenv("VISIT_SERVER_MAX_RETRIES", 1))
VISIT_SERVER_TIMEOUT = int(os.getenv("VISIT_SERVER_TIMEOUT", 180))
WEBCONTENT_MAXLENGTH = int(os.getenv("WEBCONTENT_MAXLENGTH", 100000))
VISIT_FETCH_TIMEOUT = int(os.getenv("VISIT_FETCH_TIMEOUT", 25))
VISIT_FETCH_MAX_RETRIES = int(os.getenv("VISIT_FETCH_MAX_RETRIES", 2))
VISIT_MIN_EXTRACTED_CHARS = int(os.getenv("VISIT_MIN_EXTRACTED_CHARS", 120))
VISIT_MAX_HTML_CHARS = int(os.getenv("VISIT_MAX_HTML_CHARS", 200000))
FIRECRAWL_TIMEOUT_MS = int(os.getenv("FIRECRAWL_TIMEOUT_MS", 30000))
DEFAULT_USER_AGENT = os.getenv(
    "VISIT_USER_AGENT",
    (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    ),
)


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
            self._llm_client = OpenAI(
                base_url=os.environ.get("OPENAI_BASE_URL"),
                api_key=os.environ.get("OPENAI_API_KEY")
            )
        else:
            self._llm_client = summary_client

        self._firecrawl_client = None
        firecrawl_api_key = os.getenv("FIRECRAWL_API_KEY")
        if firecrawl_api_key:
            try:
                self._firecrawl_client = Firecrawl(api_key=firecrawl_api_key)
            except Exception as exc:
                logger.warning(f"[visit] Failed to initialize Firecrawl client: {exc}")


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
            messages=msgs,
        )
        for attempt in range(max_retries):
            try:
                chat_response = await asyncio.to_thread(
                    self._llm_client.chat.completions.create,
                    **summary_args,
                )
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

    @staticmethod
    def _extract_text_from_firecrawl_doc(doc) -> str:
        if doc is None:
            return ""
        markdown = getattr(doc, "markdown", None)
        if isinstance(markdown, str) and markdown.strip():
            return markdown.strip()
        summary = getattr(doc, "summary", None)
        if isinstance(summary, str) and summary.strip():
            return summary.strip()
        html = getattr(doc, "html", None) or getattr(doc, "raw_html", None)
        if isinstance(html, str) and html.strip():
            return Visit._extract_text_from_html(html[:VISIT_MAX_HTML_CHARS])
        return ""

    def _fetch_page_text_with_firecrawl(self, url: str) -> str:
        if self._firecrawl_client is None:
            return ""
        try:
            doc = self._firecrawl_client.scrape(
                url,
                formats=["markdown"],
                only_main_content=True,
                timeout=FIRECRAWL_TIMEOUT_MS,
            )
            metadata = getattr(doc, "metadata", None)
            status_code = getattr(metadata, "status_code", None)
            error = getattr(metadata, "error", None)
            text = self._extract_text_from_firecrawl_doc(doc)
            if status_code and int(status_code) >= 400:
                logger.debug(
                    f"[visit] Firecrawl scrape returned status_code={status_code} for {url}, error={error}"
                )
                return ""
            if len(text) >= VISIT_MIN_EXTRACTED_CHARS:
                return text
            logger.debug(
                f"[visit] Firecrawl extracted content too short for {url}: {len(text)} chars"
            )
        except Exception as exc:
            logger.debug(f"[visit] Firecrawl scrape failed for {url}: {exc}")
        return ""

    @staticmethod
    def _normalize_whitespace(text: str) -> str:
        return " ".join(text.split())

    @staticmethod
    def _dedupe_lines(lines):
        seen = set()
        result = []
        for line in lines:
            normalized = Visit._normalize_whitespace(line)
            if len(normalized) < 8 or normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)
        return result

    @staticmethod
    def _extract_text_from_html(html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")

        for tag in soup(["script", "style", "noscript", "iframe", "svg", "canvas", "footer", "nav"]):
            tag.decompose()

        candidates = []
        selectors = [
            "article",
            "main",
            '[role="main"]',
            ".article",
            ".article-content",
            ".content",
            ".main",
            ".main-content",
            ".page-content",
            ".post-content",
            ".detail",
            ".news-detail",
            "body",
        ]
        for selector in selectors:
            for node in soup.select(selector):
                text = node.get_text("\n", strip=True)
                if text:
                    candidates.append(text)

        if not candidates:
            candidates.append(soup.get_text("\n", strip=True))

        best = max(candidates, key=len, default="")
        lines = Visit._dedupe_lines(best.splitlines())

        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        if title:
            lines = [title] + [line for line in lines if line != title]

        return "\n".join(lines)

    def _fetch_page_text(self, url: str) -> str:
        firecrawl_text = self._fetch_page_text_with_firecrawl(url)
        if firecrawl_text:
            return firecrawl_text

        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        last_error = None
        for attempt in range(VISIT_FETCH_MAX_RETRIES):
            try:
                response = requests.get(
                    url,
                    headers=headers,
                    timeout=VISIT_FETCH_TIMEOUT,
                    allow_redirects=True,
                )
                response.raise_for_status()

                content_type = response.headers.get("content-type", "").lower()
                if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
                    return "[visit] Failed to read page."

                html = response.text[:VISIT_MAX_HTML_CHARS]
                text = Visit._extract_text_from_html(html)
                if len(text) >= VISIT_MIN_EXTRACTED_CHARS:
                    return text
                last_error = RuntimeError(f"extracted content too short: {len(text)} chars")
            except Exception as exc:
                last_error = exc

            if attempt + 1 < VISIT_FETCH_MAX_RETRIES:
                time.sleep(1)

        logger.debug(f"html_readpage {url} failed with error {last_error}")
        return "[visit] Failed to read page."

    async def html_readpage(self, url: str, max_attempts: int = 2) -> str:
        for attempt in range(max_attempts):
            logger.debug(
                f"html_readpage {url} attempt {attempt + 1}/{max_attempts}"
            )
            content = await asyncio.to_thread(self._fetch_page_text, url)
            if (
                content
                and not content.startswith("[visit] Failed to read page.")
            ):
                return content
            await asyncio.sleep(1)
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
    """
    params =  {"url": "https://baike.baidu.com/item/%E9%99%88%E6%99%AF%E6%B6%A6/18067", "goal": "陈景润 重要获奖 成就"}
    res = asyncio.run(search.call(params))
    print(res)
    params =  {"url": "https://en.wikipedia.org/wiki/Josef_Florian", "goal": "介绍一下Josef_Florian"}
    res = asyncio.run(search.call(params))
    print(res)
    """
    import time
    t1 = time.time()
    params =  {"url": "https://travel.sina.com.cn/domestic/news/2020-02-09/detail-iimxyqvz1531129.shtml", "goal": "疫情对旅游业的影响"}
    res = asyncio.run(search.call(params))
    print(res)
    t2 = time.time()
    print(111, t2 - t1)
