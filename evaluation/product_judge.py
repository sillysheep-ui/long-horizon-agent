"""行程质量的 rubric 裁判：用二值清单代替 1–5 主观打分。

依据：
- Rubrics as Rewards（arXiv:2507.17746）：结构化 rubric 比 Likert 打分更稳，做奖励时方差更小；
- G-Eval（arXiv:2303.16634）：先写理由再给判定，比直接打分更接近人工。

裁判只回答 10 个是/否问题，每条要求引用行程里的原句作为依据；分数=通过数/10。
"""

import json
from typing import Any, Dict, List, Optional

from openai import AsyncOpenAI

from product.model_gateway import parse_json

CRITERIA: List[Dict[str, str]] = [
    {"id": "c1", "dim": "concreteness", "text": "每个活动都写明了开始时间和停留时长"},
    {"id": "c2", "dim": "concreteness", "text": "每个活动的描述写了具体看什么/做什么，而不是“感受氛围”这类空话"},
    {"id": "f1", "dim": "feasibility", "text": "同一天的活动顺序与转场在时间上走得通（含交通时间）"},
    {"id": "f2", "dim": "feasibility", "text": "单日强度适中：2–3 个活动、间隔不赶场"},
    {"id": "p1", "dim": "preference_fit", "text": "用户提到的兴趣偏好都有对应的活动安排"},
    {"id": "p2", "dim": "preference_fit", "text": "预算、同行人、节奏等约束被体现（用户没提则记通过并说明）"},
    {"id": "b1", "dim": "fact_boundary", "text": "未核实的价格、营业时间、班次都明确标注为未核实"},
    {"id": "b2", "dim": "fact_boundary", "text": "没有编造票价、营业时间或班次这类具体数字（距离、年代等描述性数字不算）"},
    {"id": "r1", "dim": "richness", "text": "不同日期的活动有明显差异，不是同一类反复堆叠"},
    {"id": "r2", "dim": "richness", "text": "每天有主次或亮点，不是简单罗列地点"},
]

JUDGE_SYSTEM_PROMPT = """你是旅行行程评审。只根据给定的用户需求和行程文本判断，不补充外部知识。
逐条回答下面 10 个问题，每条只判 pass=true/false，并用一句话说明理由，理由里必须引用行程中的原句或数字。

清单：
{c1} 具体性：每个活动都写明了开始时间和停留时长
{c2} 具体性：每个活动的描述写了具体看什么/做什么，而不是空话
{f1} 可执行性：同一天的活动顺序与转场在时间上走得通（含交通时间）
{f2} 可执行性：单日强度适中：2–3 个活动、间隔不赶场
{p1} 偏好匹配：用户提到的兴趣偏好都有对应的活动安排
{p2} 偏好匹配：预算、同行人、节奏等约束被体现（用户没提则记 pass=true 并说明）
{b1} 事实边界：未核实的价格、营业时间、班次都明确标注为未核实
{b2} 事实边界：没有编造票价、营业时间或班次这类具体数字（距离、年代等描述性数字不算）
{r1} 丰富度：不同日期的活动有明显差异，不是同一类反复堆叠
{r2} 丰富度：每天有主次或亮点，不是简单罗列地点

只输出 JSON，不要输出其他文字：
{{"criteria": {{"c1": {{"pass": true, "reason": "…"}}, "c2": {{"pass": false, "reason": "…"}}, "f1": {{"pass": true, "reason": "…"}}, "f2": {{"pass": true, "reason": "…"}}, "p1": {{"pass": true, "reason": "…"}}, "p2": {{"pass": true, "reason": "…"}}, "b1": {{"pass": true, "reason": "…"}}, "b2": {{"pass": true, "reason": "…"}}, "r1": {{"pass": true, "reason": "…"}}, "r2": {{"pass": true, "reason": "…"}}}}, "comments": ["最需要改的一点"]}}""".format(**{item["id"]: item["text"] for item in CRITERIA})


def score_of(parsed: Dict[str, Any]) -> Optional[float]:
    criteria = parsed.get("criteria") or {}
    hits = [1 for item in CRITERIA if (criteria.get(item["id"]) or {}).get("pass") is True]
    if not criteria:
        return None
    return round(len(hits) / len(CRITERIA), 3)


def fabrication_of(parsed: Dict[str, Any]) -> bool:
    criteria = parsed.get("criteria") or {}
    return (criteria.get("b2") or {}).get("pass") is False


def _parse_loose(content: str) -> Dict[str, Any]:
    """先走通用解析；失败时允许字符串内有裸换行（模型常见毛病）。"""
    try:
        return parse_json(content)
    except Exception:
        start, end = content.find("{"), content.rfind("}")
        if start < 0 or end <= start:
            raise
        return json.loads(content[start:end + 1], strict=False)


async def judge(client: AsyncOpenAI, model: str, case: Dict[str, Any], compact: Dict[str, Any]) -> Dict[str, Any]:
    payload = json.dumps({"query": case["query"], "itinerary": compact}, ensure_ascii=False)
    last_error = ""
    for attempt in range(2):
        try:
            response = await client.chat.completions.create(
                model=model, temperature=0.0,
                messages=[{"role": "system", "content": JUDGE_SYSTEM_PROMPT},
                          {"role": "user", "content": payload + "\n\n只输出 JSON。"
                           + ("字符串内不要换行，直接给判定。" if attempt else "")}],
                max_tokens=4000,
            )
        except Exception as error:  # 裁判失败不能拖垮整轮评测
            return {"error": str(error)[:300], "rubric": None}
        content = response.choices[0].message.content or ""
        if response.choices[0].finish_reason == "length":
            last_error = "裁判输出被截断"
            continue
        try:
            parsed = _parse_loose(content)
        except Exception as error:
            last_error = f"裁判 JSON 解析失败：{str(error)[:200]}"
            continue
        criteria = parsed.get("criteria") or {}
        failed = [item["id"] for item in CRITERIA if (criteria.get(item["id"]) or {}).get("pass") is not True]
        return {"rubric": score_of(parsed), "fabrication": fabrication_of(parsed),
                "failed": failed, "comments": parsed.get("comments") or []}
    return {"error": last_error or "裁判失败", "rubric": None}
