"""大事记数据规范化：别名合并、无效键过滤、脏数据剔除。"""

from __future__ import annotations

import re
from typing import Any

# 历史种子 events 中与政权键不一致的名称 → 标准政权键（与 GeoJSON NAME 一致）
EVENT_KEY_ALIASES: dict[str, str] = {
    "Aksumite Empire": "Axum",
    "Arab Caliphate": "Abbasid Caliphate",
    "Assyrian Empire": "Assyria",
    "Babylonian Empire": "Babylonia",
    "Burmese kingdoms": "Burma",
    "Ethiopian Empire": "Ethiopia",
    "French Empire": "France",
    "Ghana Empire": "Empire of Ghana",
    "Han Dynasty": "Han",
    "Kushite Empire": "Kush",
    "Mali Empire": "Mali",
    "Mayan civilization": "Maya city-states",
    "Ming": "Ming Empire",
    "Persian Empire": "Persia",
    "Phoenician city-states": "Phoenicia",
    "Qing": "Qing Empire",
    "Siamese kingdoms": "Thailand",
    "Song": "Song Empire",
    "Songhai Empire": "Songhai",
    "Spanish Empire": "Spain",
    "Tang": "Tang Empire",
    "Vietnamese dynasties": "Vietnam",
    "Israelite kingdoms": "Israel",
}

# 非单一政权条目，构建时丢弃
DROP_EVENT_KEYS = frozenset({
    "Andean civilizations",
    "Caucasian kingdoms",
    "Central Asian nomads",
    "Hellenistic kingdoms",
    "Indian Ocean trade",
    "Mesoamerican civilizations",
    "North American cultures",
    "Pacific cultures",
    "Polynesian expansion",
    "Siberian peoples",
    "Silk Road",
    "Steppe empires",
    "Swahili Coast",
    "Swahili civilization",
    "Teotihuacan",
    "Viking Age",
    "West African kingdoms",
})

_GARBAGE = re.compile(r"en_en|event_name|<0x", re.I)


def normalize_event_text(value: Any) -> str:
    """将 event 字段规范为字符串；模型偶发返回 bool/null 时安全跳过。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value).strip()
    return ""


def is_valid_event_text(text: str) -> bool:
    text = (text or "").strip()
    if not text or len(text) > 200:
        return False
    return _GARBAGE.search(text) is None


def merge_event_lists(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_year: dict[int, dict[str, Any]] = {}
    for item in existing + incoming:
        if not isinstance(item, dict):
            continue
        try:
            year = int(item["year"])
        except (KeyError, TypeError, ValueError):
            continue
        event = normalize_event_text(item.get("event"))
        if not is_valid_event_text(event):
            continue
        prev = by_year.get(year)
        if prev is None or len(event) > len(prev["event"]):
            by_year[year] = {"year": year, "event": event}
    return sorted(by_year.values(), key=lambda x: x["year"])


def resolve_event_key(key: str, regime_names: set[str]) -> str | None:
    """将 events 键解析为 regime 键；无法解析则返回 None。"""
    if key in DROP_EVENT_KEYS:
        return None
    if key in regime_names:
        return key
    alias = EVENT_KEY_ALIASES.get(key)
    if alias and alias in regime_names:
        return alias
    return None


def normalize_events(
    raw_events: dict[str, list[dict[str, Any]]],
    regime_names: set[str],
) -> dict[str, list[dict[str, Any]]]:
    """仅保留 regime_names 下的大事记，合并别名并去重。"""
    merged: dict[str, list[dict[str, Any]]] = {name: [] for name in regime_names}

    for key, items in raw_events.items():
        if not items:
            continue
        target = resolve_event_key(key, regime_names)
        if target is None:
            continue
        merged[target] = merge_event_lists(merged.get(target, []), items)

    return {k: v for k, v in merged.items() if v}
