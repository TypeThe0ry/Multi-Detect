"""Whether a generated task text describes an object other than its true target.

Generated tasks name the target by its category (Chinese text) or its first
semantic word (English text) plus an optional colour. Another object fits the
same text when it shares that category or carries that word, and its colour
does not contradict the requested one (an unknown colour cannot rule it out).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

CATEGORY_ZH: dict[str, str] = {
    "car": "\u8f7f\u8f66",
    "suv": "SUV",
    "pickup": "\u76ae\u5361",
    "ambulance": "\u6551\u62a4\u8f66",
    "fire_truck": "\u6d88\u9632\u8f66",
    "bus": "\u516c\u4ea4\u8f66",
    "person": "\u884c\u4eba",
    "traffic_cone": "\u4ea4\u901a\u9525",
    "barrel": "\u8def\u969c\u6876",
    "jersey_barrier": "\u9694\u79bb\u58a9",
    "fire_hydrant": "\u6d88\u9632\u6813",
    "dumpster": "\u5783\u573e\u7bb1",
    "electrical_box": "\u7535\u7bb1",
    "mailbox": "\u90ae\u7bb1",
    "postbox": "\u90ae\u7b52",
    "cardboard_box": "\u7eb8\u7bb1",
    "backpack": "\u80cc\u5305",
    "stop_sign": "\u505c\u6b62\u6807\u5fd7",
}
COLOR_ZH: dict[str, str] = {
    "red": "红色",
    "blue": "蓝色",
    "white": "白色",
    "orange": "橙色",
    "brown": "棕色",
}


def fuel_task_texts(obj: Mapping[str, Any]) -> tuple[str, str]:
    """(Chinese, English) "find and record" task texts that name ``obj``.

    The same wording the fuel world generator writes into the index, so a task
    can be phrased for any catalogued object (true target or not).
    """
    words = list(obj.get("semantic_words") or obj.get("category_words") or [])
    category = obj.get("category") or obj.get("class")
    color = obj.get("color")
    category_zh = CATEGORY_ZH.get(category, words[0])
    chinese = f"找到{COLOR_ZH.get(color, '')}{category_zh}并记录位置"
    english = " ".join(
        ["Find the", *([color] if color else []), words[0], "and record its position."]
    )
    return chinese, english


# Typical largest horizontal extent (m) of common objects: host knowledge used to
# pick a search altitude at which an object of that kind is big enough to detect.
TYPICAL_SIZE_M: dict[str, float] = {
    "stop sign": 0.8,
    "停止标志": 0.8,
    "停车标志": 0.8,
    "sign": 0.8,
    "traffic cone": 0.5,
    "cone": 0.5,
    "交通锥": 0.5,
    "锥桶": 0.5,
    "fire hydrant": 0.6,
    "hydrant": 0.6,
    "消防栓": 0.6,
    "cardboard box": 0.6,
    "box": 0.6,
    "纸箱": 0.6,
    "backpack": 0.5,
    "bag": 0.5,
    "背包": 0.5,
    "mailbox": 0.6,
    "邮箱": 0.6,
    "postbox": 0.7,
    "邮筒": 0.7,
    "person": 0.6,
    "pedestrian": 0.6,
    "行人": 0.6,
    "人": 0.6,
    "barrel": 0.6,
    "路障桶": 0.6,
    "electrical box": 1.0,
    "电箱": 1.0,
    "dumpster": 2.0,
    "垃圾箱": 2.0,
    "jersey barrier": 3.0,
    "隔离墩": 3.0,
    "car": 4.5,
    "轿车": 4.5,
    "suv": 4.8,
    "pickup": 5.5,
    "皮卡": 5.5,
    "truck": 7.0,
    "bus": 11.0,
    "公交车": 11.0,
    "ambulance": 6.0,
    "救护车": 6.0,
    "fire truck": 9.0,
    "消防车": 9.0,
}


def typical_object_size(task_text: str, default: float = 2.0) -> float:
    """Typical size of the object a task names (longest matching phrase), else ``default``."""
    lowered = task_text.lower()
    matches = [key for key in TYPICAL_SIZE_M if key in lowered]
    if not matches:
        return default
    return TYPICAL_SIZE_M[max(matches, key=len)]


def fuel_count_texts(obj: Mapping[str, Any]) -> tuple[str, str]:
    """(Chinese, English) counting task texts for ``obj``'s kind."""
    words = list(obj.get("semantic_words") or obj.get("category_words") or [])
    category = obj.get("category") or obj.get("class")
    color = obj.get("color")
    category_zh = CATEGORY_ZH.get(category, words[0])
    chinese = f"数一数有几个{COLOR_ZH.get(color, '')}{category_zh}并报告数量和位置"
    english = " ".join(
        ["Count the", *([color] if color else []), words[0], "objects and report their positions."]
    )
    return chinese, english


def task_equivalent(target: Mapping[str, Any], other: Mapping[str, Any]) -> bool:
    """True when the task text generated for ``target`` also describes ``other``."""
    words = list(target.get("category_words") or target.get("semantic_words") or [])
    other_words = set(other.get("category_words") or other.get("semantic_words") or [])
    category = target.get("class") or target.get("category")
    other_category = other.get("class") or other.get("category")
    same_kind = (bool(words) and words[0] in other_words) or (
        category is not None and category == other_category
    )
    if not same_kind:
        return False
    color = target.get("color")
    return color is None or other.get("color") in (None, color)


__all__ = [
    "TYPICAL_SIZE_M",
    "typical_object_size",
    "CATEGORY_ZH",
    "COLOR_ZH",
    "fuel_count_texts",
    "fuel_task_texts",
    "task_equivalent",
]
