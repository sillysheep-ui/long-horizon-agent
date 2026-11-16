"""同一接口连接本地 MLX 服务和外部兼容 API。"""

import json
import os
import re
from typing import Any

import httpx


class ModelUnavailable(RuntimeError):
    pass


class ModelGateway:
    def __init__(self) -> None:
        legacy_base = os.getenv("OPENAI_BASE_URL", "")
        legacy_is_deepseek = "api.deepseek.com" in legacy_base
        self.routes = [
            ("local", os.getenv("LOCAL_MODEL_BASE_URL", "http://127.0.0.1:8080/v1"),
             os.getenv("LOCAL_MODEL_NAME", "local-qwen3-4b"), os.getenv("LOCAL_MODEL_API_KEY", "local")),
            ("deepseek", os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
             os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
             os.getenv("DEEPSEEK_API_KEY") or (os.getenv("OPENAI_API_KEY", "") if legacy_is_deepseek else "")),
            ("api", os.getenv("MODEL_API_BASE_URL", ""), os.getenv("MODEL_API_NAME", ""),
             os.getenv("MODEL_API_KEY", "")),
            ("openai_compatible", "" if legacy_is_deepseek else legacy_base,
             os.getenv("AGENT_MODEL_ID") or os.getenv("LLM_MODEL_ID", ""), os.getenv("OPENAI_API_KEY", "")),
        ]
        profile = os.getenv("MODEL_PROFILE", "local_first")
        if profile == "api_first":
            self.routes = self.routes[1:] + self.routes[:1]
        elif profile == "api_only":
            self.routes = self.routes[1:]
        elif profile == "local_first" and not os.getenv("LOCAL_MODEL_BASE_URL") and (
                os.getenv("MODEL_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("DEEPSEEK_API_KEY")):
            # 本地服务未显式配置时，沿用项目已有 API 配置以避免每轮空等连接超时。
            self.routes = self.routes[1:] + self.routes[:1]
        try:
            self.timeout_seconds = float(os.getenv("MODEL_TIMEOUT_SECONDS", "40"))
        except ValueError:
            self.timeout_seconds = 40.0

    async def complete(self, role: str, system: str, user: str, max_tokens: int = 1200) -> dict[str, str]:
        failures: list[str] = []
        for name, base, model, key in self.routes:
            if not base or not model or (name != "local" and not key):
                continue
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(
                        self.timeout_seconds, connect=min(5.0, self.timeout_seconds))) as client:
                    body = {"model": model, "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}], "temperature": 0,
                            "max_tokens": max_tokens}
                    if name == "deepseek":
                        body["reasoning_effort"] = "none"
                        body["response_format"] = {"type": "json_object"}
                    response = await client.post(base.rstrip("/") + "/chat/completions",
                        headers={"Authorization": "Bearer " + key},
                        json=body)
                    response.raise_for_status()
                    choice = response.json()["choices"][0]
                    content = choice["message"]["content"]
                    if choice.get("finish_reason") == "length":
                        raise ValueError("模型输出超出长度限制")
                    if not content or not content.strip():
                        raise ValueError("模型返回空内容")
                    return {"content": content, "provider": name, "model": model, "role": role}
            except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
                failures.append(name + ": " + type(exc).__name__)
        raise ModelUnavailable("模型服务均不可用；请启动本地 MLX 或配置外部 API。" + ", ".join(failures))


def parse_json(text: str) -> dict[str, Any]:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("模型没有输出 JSON 对象") from exc
        value = json.loads(text[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("模型输出不是 JSON 对象")
    return value
