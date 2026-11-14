"""把网页标题清洗成可展示的地点名。

搜索结果是网页标题（携程攻略、Trip.com 之类），直接当地点名会得到
“杭州龙井村游玩攻略简介,杭州龙井村门票/地址…”这样的字符串。这里只做
确定性的后缀裁剪，并保留原始标题供溯源，不做实体识别猜测。
"""

import re
from typing import List

SEPARATORS = ("｜", "|", "【", "（", "(", " - ", "—", "_", "，", ",", "：")
TRAILING_WORDS = (
    "游玩攻略简介", "游玩攻略", "旅游攻略", "自由行攻略", "攻略简介", "攻略",
    "门票价格", "门票", "开放时间", "营业时间", "地址", "图片", "照片",
    "点评", "怎么样", "好玩吗", "一日游", "简介", "大全", "预订",
    "探店指南", "探店", "指南", "推荐", "合集", "必去", "打卡",
)
TRAILING_PUNCT = " ·-—~～、。!！?？:："
GENERIC_LABELS = {"定位", "地址", "位置", "电话", "营业时间", "详情", "导航"}


def clean_place_name(title: str) -> str:
    """裁掉站点后缀、栏目词和标点，得到较短的地点名。"""
    text = str(title or "").strip()
    if not text:
        return ""
    text = re.sub(r"^[^\w\u4e00-\u9fff]+", "", text)
    for separator in SEPARATORS:
        if separator in text:
            text = text.split(separator, 1)[0]
    changed = True
    while changed and text:
        changed = False
        text = text.rstrip(TRAILING_PUNCT)
        for word in TRAILING_WORDS:
            if text.endswith(word) and len(text) > len(word) + 1:
                text = text[: -len(word)]
                changed = True
                break
    text = re.sub(r"\s+", " ", text).strip()
    if text in GENERIC_LABELS:
        return ""
    return text or str(title).strip()[:40]


def looks_like_place(name: str, keyword: str, city: str) -> bool:
    """短名、含关键词或城市名的标题更像地点，长文章标题降权。"""
    text = str(name or "")
    if any(token in text for token in ("的", "我", "被", "了", "～", "!")):
        return False
    if len(text) <= 8:
        return True
    if keyword and keyword in text and len(text) <= len(keyword) + 8:
        return True
    if city and city in text and len(text) <= 20:
        return True
    return False


def dedupe_names(items: List[dict]) -> List[dict]:
    seen = set()
    result = []
    for item in items:
        name = str(item.get("name") or "")
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(item)
    return result


def _bigrams(text: str) -> set:
    cleaned = "".join(ch for ch in text if ch.isalnum())
    return {cleaned[i:i + 2] for i in range(len(cleaned) - 1)}


def title_matches(title: str, names: List[str], threshold: float = 0.6) -> bool:
    """标题是否落在证据地点名上：包含关系，或 2-gram 重合度达到阈值。"""
    normalized = "".join(ch for ch in str(title or "") if ch.isalnum())
    if not normalized:
        return False
    grams = _bigrams(normalized)
    for name in names:
        candidate = "".join(ch for ch in str(name or "") if ch.isalnum())
        if not candidate:
            continue
        if normalized in candidate or candidate in normalized:
            return True
        if grams and len(grams & _bigrams(candidate)) / len(grams) >= threshold:
            return True
    return False
