"""Create a deterministic two-task campaign variant for order-balanced SIM.

Only campaign priorities change.  The task/runtime documents, embedded
ranker coefficients, airframe profile, safety policy, and Mission compiler
inputs remain byte-for-byte equivalent at the semantic level.  This is for
counterbalanced evaluation, not for changing a policy artifact.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def make_variant(source: dict[str, Any], *, first_task_id: str) -> dict[str, Any]:
    if source.get("schema_version") != 1 or not isinstance(source.get("tasks"), list):
        raise ValueError("unsupported campaign schema")
    tasks = source["tasks"]
    if len(tasks) != 2:
        raise ValueError("order-balanced variant requires exactly two tasks")
    entries = [item.get("task", {}) for item in tasks]
    ids = [item.get("task_id") for item in entries]
    if first_task_id not in ids or len(set(ids)) != 2:
        raise ValueError("first task must name one of two unique task IDs")
    ranker = source.get("ranker", {})
    coefficient = ranker.get("coefficients", {}).get("priority")
    if coefficient not in (-1, 1):
        raise ValueError("campaign ranker priority coefficient must be -1 or 1")
    second_task_id = next(task_id for task_id in ids if task_id != first_task_id)
    desired_priority = 10 if coefficient == 1 else 1
    other_priority = 1 if coefficient == 1 else 10
    result = json.loads(json.dumps(source))
    for item in result["tasks"]:
        task_id = item["task"]["task_id"]
        item["priority"] = desired_priority if task_id == first_task_id else other_priority
    # Keep the original task list order; selection, not serialization order,
    # must establish the requested first task.
    assert second_task_id in {item["task"]["task_id"] for item in result["tasks"]}
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--first-task", required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("output must be new")
    source = json.loads(args.input.read_text(encoding="utf-8"))
    result = make_variant(source, first_task_id=args.first_task)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"first_task": args.first_task, "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
