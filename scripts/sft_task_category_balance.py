"""Shared deterministic task-category positive upsampling for slow-system SFT."""

from __future__ import annotations

import copy
import math
import random
from collections import defaultdict
from collections.abc import Callable, Iterable

POSITIVE_DECISIONS = frozenset({"confirm_target", "approach_track"})
NEGATIVE_DECISIONS = frozenset({"reject_track"})


def _validate(min_ratio: float, max_upsample: int) -> None:
    if not math.isfinite(min_ratio) or min_ratio < 0:
        raise ValueError("min_ratio must be finite and non-negative")
    if max_upsample < 1:
        raise ValueError("max_upsample must be at least 1")


def balance_task_categories(
    rows: Iterable[dict], *, min_ratio: float, max_upsample: int, seed: int,
    category_for_row: Callable[[dict], str | None] | None = None,
) -> tuple[list[dict], dict[str, dict[str, int]]]:
    """Upsample identity positives per category, leaving all other rows intact.

    The returned rows retain their input order, with deterministic duplicates
    appended. Duplicate IDs receive a ``~task-balance-N`` suffix.
    """
    _validate(min_ratio, max_upsample)
    source = list(rows)
    category_for_row = category_for_row or (lambda row: row.get("task_category"))
    groups: dict[str, dict[str, list[dict]]] = defaultdict(lambda: {"positive": [], "negative": []})
    for row in source:
        category = category_for_row(row)
        decision = (row.get("assistant_target") or {}).get("decision")
        if category and decision in POSITIVE_DECISIONS:
            groups[str(category)]["positive"].append(row)
        elif category and decision in NEGATIVE_DECISIONS:
            groups[str(category)]["negative"].append(row)

    result = list(source)
    report: dict[str, dict[str, int]] = {}
    rng = random.Random(seed)
    for category in sorted(groups):
        positive = groups[category]["positive"]
        negative = groups[category]["negative"]
        original_positive = len(positive)
        original_negative = len(negative)
        target = original_positive
        if original_positive and original_negative:
            target = min(
                max(original_positive, math.ceil(min_ratio * original_negative)),
                max_upsample * original_positive,
            )
        duplicates = target - original_positive
        for index in range(duplicates):
            duplicate = copy.deepcopy(positive[rng.randrange(original_positive)])
            duplicate["sample_id"] = f"{duplicate['sample_id']}~task-balance-{index + 1}"
            result.append(duplicate)
        report[category] = {
            "positive_before": original_positive,
            "negative_before": original_negative,
            "positive_after": target,
            "negative_after": original_negative,
            "duplicates_added": duplicates,
        }
    return result, report


