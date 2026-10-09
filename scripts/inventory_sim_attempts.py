"""Inventory explicitly supplied outer reports; acceptance is not policy success."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.serialization import strict_json_loads  # noqa: E402
from scripts.audit_mission_clock_receipts import read_bound  # noqa: E402


def classify_outer(report):
    if report.get("schema") != "multidetect.vtol-mission-sensor-capture":
        raise ValueError("expected single capture outer report")
    execution = report.get("execution", {})
    requested, status = execution.get("requested"), execution.get("status")
    gate = report.get("gate_passed")
    if requested is False and status == "planned_not_executed" and gate is False:
        return "planned_not_executed"
    if requested is True and status == "passed" and gate is True and report.get("failure") is None:
        return "execution_acceptance_passed"
    if requested is True and status == "failed" and gate is False and report.get("failure"):
        return "execution_acceptance_rejected"
    return "unresolved_or_inconsistent"


def inventory(inputs):
    if not 1 <= len(inputs) <= 512:
        raise ValueError("1..512 explicit reports required")
    entries, seen_hashes, seen_containers, counts = [], set(), set(), {}
    for path, digest in inputs:
        raw = read_bound(path, digest)
        report = strict_json_loads(raw.decode("utf-8"))
        category = classify_outer(report)
        container = report.get("container", {}).get("id")
        if digest in seen_hashes or (container and container in seen_containers):
            raise ValueError("duplicate report or container attempt")
        seen_hashes.add(digest)
        if container:
            seen_containers.add(container)
        counts[category] = counts.get(category, 0) + 1
        entries.append(
            {
                "path": str(path),
                "sha256": digest,
                "container_id": container,
                "category": category,
                "reported_failure": report.get("failure"),
            }
        )
    return {
        "schema_version": 1,
        "entries": entries,
        "counts": counts,
        "scope": "only explicitly supplied hash-bound outer reports",
        "all_project_attempts_included": False,
        "underlying_gates_revalidated": False,
        "task_success_rate": None,
        "policy_comparison_valid": False,
        "training_eligible": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report", nargs=2, action="append", required=True, metavar=("PATH", "SHA256")
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = inventory(args.report)
    result["evaluator_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps(result["counts"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
