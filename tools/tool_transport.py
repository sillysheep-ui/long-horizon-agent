# -*- coding: utf-8 -*-
# --------------------------------------------
# 文件描述: 机票查询工具 
# --------------------------------------------


import asyncio
import logging
import os
import json
import httpx
from openai import AsyncOpenAI
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

logger = logging.getLogger(__name__)

from prompts.prompt import TRANSPORT_SYSTEM_PROMPT
from qwen_agent.tools.base import BaseTool, register_tool


@register_tool("flights_search", allow_overwrite=True)
class FlightsSearch(BaseTool):
    name = "flights_search"
    description = "根据日期查询城市间航班信息。"
    parameters = {
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "description": "日期，格式 YYYY-MM-DD"
            },
            "from_city": {
                "type": "string",
                "description": "出发城市中文名"
            },
            "to_city": {
                "type": "string",
                "description": "到达城市中文名"
            },
        },
        "required": ["date", "from_city", "to_city"]
    }

    def __init__(self, cfg: dict | None = None):
        super().__init__(cfg)

    async def call(self, params: dict, **kwargs) -> str:  # type: ignore[override]
        try:
            date = params["date"]
            from_city = params["from_city"]
            to_city = params["to_city"]
        except Exception:
            return "[FlightsSearch] Invalid request format: Input must be a JSON object containing 'date' and 'from_city' and 'to_city' field"


        kwargs = {"date": date, "from_city": from_city, "to_city": to_city}
        query = json.dumps(kwargs, ensure_ascii=False)
        messages = [
            {"role": "system", "content": TRANSPORT_SYSTEM_PROMPT},
            {"role": "user", "content": query},
        ]

        result = None
        async with AsyncOpenAI(
            api_key=os.environ["OPENAI_API_KEY"],
            base_url=os.environ["OPENAI_BASE_URL"],
            timeout=25,
            max_retries=0,
        ) as client:

            try:
                response = await client.chat.completions.create(
                    messages=messages,
                    model=os.environ["LLM_MODEL_ID"],
                    max_tokens=1200,
                    temperature=0,
                    response_format={"type": "json_object"},
                    extra_body={"thinking": {"type": "disabled"}},
                )
                result = response.choices[0].message.content.strip()
            except Exception as e:
                logger.error(f"[flights_search] Failed to get response: {e}")
                result = f"API response error: flights_search: {type(e).__name__}: {e}"
            finally:
                await client.close()

        if not result:
            return f"两地无航班信息:{params}"

        return result


if __name__ == "__main__":
    search = FlightsSearch()
    params = {"date": "2026-03-30", "from_city": "成都", "to_city": "北京"}
    res = asyncio.run(search.call(params))
    print(res)
