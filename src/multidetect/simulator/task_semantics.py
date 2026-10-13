"""Whether a generated task text describes an object other than its true target.

Generated tasks name the target by its category (Chinese text) or its first
semantic word (English text) plus an optional colour. Another object fits the
same text when it shares that category or carries that word, and its colour
does not contradict the requested one (an unknown colour cannot rule it out).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


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


__all__ = ["task_equivalent"]
