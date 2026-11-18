"""把底层异常翻译成用户看得懂、能照做的话。"""

from __future__ import annotations

import asyncio

import httpx

from product.model_gateway import ModelUnavailable

RETRY_HINT = "再发一次就行。"

# 这些错误重发一次有意义；输入类错误重发同样的内容没用。
RETRYABLE = {"upstream_unavailable", "network", "timeout", "rate_limited"}


def _text(exc: BaseException) -> str:
    return str(exc) or exc.__class__.__name__


def friendly_error(exc: BaseException) -> dict[str, object]:
    """返回 {code, message, retryable}，供任务失败时展示。"""
    text = _text(exc)

    if isinstance(exc, asyncio.TimeoutError | TimeoutError) or "timed out" in text.lower():
        return {"code": "timeout", "retryable": True,
                "message": "这次规划超时了，已经停下。" + RETRY_HINT}

    if isinstance(exc, httpx.ConnectError):
        return {"code": "network", "retryable": True,
                "message": "连不上模型服务，可能是网络断了。" + RETRY_HINT}

    if isinstance(exc, httpx.ReadTimeout | httpx.ConnectTimeout | httpx.WriteTimeout):
        return {"code": "timeout", "retryable": True,
                "message": "模型服务响应太慢，已经停止等待。" + RETRY_HINT}

    if "429" in text or "rate limit" in text.lower():
        return {"code": "rate_limited", "retryable": True,
                "message": "模型服务有点忙，请稍等半分钟再试。"}

    if "模型输出超出长度限制" in text:
        return {"code": "too_long", "retryable": False,
                "message": "这次内容太长，模型没能在限定长度内写完。把需求说得简短些再试。"}

    if isinstance(exc, ModelUnavailable):
        if "ConnectError" in text:
            return {"code": "network", "retryable": True,
                    "message": "连不上模型服务，可能是网络断了。" + RETRY_HINT}
        if "Timeout" in text:
            return {"code": "timeout", "retryable": True,
                    "message": "模型服务响应太慢，已经停止等待。" + RETRY_HINT}
        return {"code": "upstream_unavailable", "retryable": True,
                "message": "模型服务暂时不可用（已自动重试）。" + RETRY_HINT}

    if "模型服务暂时不可用" in text or "ModelUnavailable" in text:
        return {"code": "upstream_unavailable", "retryable": True,
                "message": "模型服务暂时不可用（已自动重试）。" + RETRY_HINT}

    if "规划 Agent 返回的天数与需求不一致" in text:
        return {"code": "bad_plan", "retryable": False,
                "message": "这次行程没排明白（天数对不上），换个说法再试一次。"}

    if isinstance(exc, ValueError):
        return {"code": "bad_request", "retryable": False,
                "message": "这次的需求我没理解清楚，换个说法再试一次。"}

    # 内部错误：不把堆栈丢给用户，只在日志里保留。
    return {"code": "internal", "retryable": True,
            "message": "服务出了点问题，稍后重试即可。"}
