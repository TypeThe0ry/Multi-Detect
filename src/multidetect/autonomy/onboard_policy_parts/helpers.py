"""Small parsing and rendering helpers used by the onboard policy."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from multidetect.autonomy.mission_plan import MissionStep


def _observation_timestamp_s(observation: Mapping[str, Any]) -> float | None:
    """Use the collector's pose/Gazebo simulation stamps, never receive time."""
    for key, units_per_s in (
        ("pose_time_boot_ms", 1_000.0),
        ("pose_sim_stamp_ns", 1_000_000_000.0),
        ("frame_sensor_stamp_ns", 1_000_000_000.0),
    ):
        value = observation.get(key)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value >= 0
        ):
            return value / units_per_s
    return None


def _vehicle_receipt_s(sample: Mapping[str, Any]) -> float | None:
    """Vehicle (PX4 boot) clock at a telemetry sample's receipt, if stamped."""
    value = sample.get("received_vehicle_s")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value >= 0 else None


def _enabled(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return value is True


def perception_task_text(
    step: MissionStep,
    original: str,
    *,
    task_text_for: Callable[[MissionStep], str] | None = None,
) -> str:
    """Use a plan query for slow perception unless the query is non-ASCII."""
    query = step.params.get("query") if step is not None else None
    if isinstance(query, str) and query.isascii():
        if task_text_for is not None:
            return task_text_for(step)
        return f"Find the {query} and record its position."
    return original


def labelled_refs(
    refs: Mapping[str, Any] | Sequence[str] | None, labels: Mapping[str, Any]
) -> tuple[str, ...]:
    """Planner refs with slash-separated aliases kept in the package string."""
    rendered = []
    for ref in refs or ():
        if ref not in labels:
            rendered.append(str(ref))
            continue
        value = labels[ref]
        if isinstance(value, (list, tuple)):
            value = " / ".join(str(item).strip() for item in value if str(item).strip())
        rendered.append(f"{ref} ({value})")
    return tuple(rendered)
