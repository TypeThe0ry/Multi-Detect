"""Admit hash-bound SIM completion/host-duration records, not pixel or energy labels."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.serialization import (  # noqa: E402
    canonical_sha256,
    require_sha256,
    require_text,
    strict_json_loads,
)
from multidetect.clock import validate_host_identity  # noqa: E402
from scripts.audit_mission_clock_receipts import read_bound  # noqa: E402
from scripts.run_vtol_mission_sensor_capture import validate_mission_journal  # noqa: E402


def admit(outer_path, outer_sha, mission_path, mission_sha, journal_path, journal_sha):
    outer = strict_json_loads(read_bound(outer_path, outer_sha).decode("utf-8"))
    mission = strict_json_loads(read_bound(mission_path, mission_sha).decode("utf-8"))
    read_bound(journal_path, journal_sha)
    final = outer.get("final_integrity", {})
    if (
        outer.get("schema") != "multidetect.vtol-mission-sensor-capture"
        or outer.get("gate_passed") is not True
        or outer.get("failure") is not None
        or final.get("revalidated_after_mission_and_cleanup") is not True
        or final.get("hashes", {}).get("mission_evidence") != mission_sha
        or final.get("hashes", {}).get("mission_journal") != journal_sha
        or outer.get("mission", {}).get("file_sha256") != mission_sha
        or any(
            outer.get("gates", {}).get(k) is not True
            for k in (
                "complete_vtol_mission",
                "mission_journal_integrity",
                "final_artifact_integrity",
            )
        )
    ):
        raise ValueError("outer execution binding failed")
    if (
        mission.get("schema") != "multidetect.px4-vtol-sitl-mission-acceptance"
        or mission.get("gate_passed") is not True
        or mission.get("failure") is not None
        or mission["container"]["id"] != outer["container"]["id"]
        or any(
            mission.get("safety", {}).get(k) is not False
            for k in (
                "real_pixhawk_contacted",
                "hardware_control_enabled",
                "ground_truth_used_for_mission",
            )
        )
    ):
        raise ValueError("successful SIM execution required")
    checks = validate_mission_journal(
        Path(journal_path),
        mission,
        transaction_id=mission["transaction"]["id"],
        committed_protocol_sha256=mission["protocol"]["committed_protocol_sha256"],
    )
    if not checks or not all(value is True for value in checks.values()):
        raise ValueError("journal validation failed")
    # The validator reopens the explicit local path. Verify unchanged bytes.
    read_bound(journal_path, journal_sha)
    flight, window = mission["flight"], mission["mission_receive_window"]
    identity = validate_host_identity(window.get("host_clock_identity"))
    if (
        window.get("clock") != "host_perf_counter_since_collector_start"
        or flight.get("host_clock_identity") != identity
        or window.get("available") is not True
        or window.get("start_semantics") != "before_ARM_command_after_permit"
        or window.get("end_semantics") != "accepted_landed_disarmed_observation"
        or any(
            flight.get(k) is not True
            for k in (
                "auto_mission_confirmed",
                "armed_after_permit",
                "in_air_observed",
                "fixed_wing_observed",
                "multicopter_observed",
                "landed_observed",
                "terminal_disarmed",
            )
        )
    ):
        raise ValueError("completion state or host clock binding failed")
    arm = window["arm_request_host_monotonic_s"]
    end = window["completed_host_monotonic_s"]
    origin = flight["observer_origin_host_monotonic_s"]
    duration = flight["duration_s"]
    values = (arm, end, origin, duration)
    if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in values):
        raise ValueError("invalid execution times")
    outcome = mission["shared_outcome_shadow"]["outcome"]
    if (
        not arm <= origin < end
        or flight["completed_host_monotonic_s"] != end
        or not math.isclose(end - origin, duration, rel_tol=0, abs_tol=1e-5)
        or outcome.get("status") != "completed"
        or outcome.get("mission_hash") != mission["documents"]["executable_mission"]["sha256"]
        or type(outcome.get("elapsed_s")) not in (int, float)
        or not math.isclose(outcome["elapsed_s"], duration, rel_tol=0, abs_tol=1e-5)
    ):
        raise ValueError("outcome identity or duration mismatch")
    scenario = mission["scenario"]
    scenario_id = require_text(scenario["scenario_id"], "scenario_id")
    manifest_sha = require_sha256(scenario["manifest_sha256"], "manifest_sha256")
    if (
        outer.get("scenario", {}).get("scenario_id_expected") != scenario_id
        or outer["scenario"].get("manifest_sha256") != manifest_sha
    ):
        raise ValueError("scenario binding mismatch")
    task_semantics = dict(mission["documents"]["task_spec"]["document"])
    task_semantics.pop("task_id", None)
    return {
        "schema_version": 1,
        "record_kind": "successful_sim_execution_receive_window",
        "inputs": {
            "outer_sha256": outer_sha,
            "mission_sha256": mission_sha,
            "journal_sha256": journal_sha,
        },
        "mission_sha256": outcome["mission_hash"],
        "grouping": {
            "scenario_id": scenario_id,
            "scenario_manifest_sha256": manifest_sha,
            "task_semantics_sha256": canonical_sha256(task_semantics),
            "host_session_id": identity["session_id"],
        },
        "host_clock_identity": identity,
        "labels": {"completed": True, "arm_to_completion_host_receive_s": end - arm},
        "journal_checks": checks,
        "eligible_uses": [
            "offline_sim_completion_comparison",
            "same_semantics_host_receive_duration_comparison",
        ],
        "synchronized_pixel_labels_eligible": False,
        "physical_energy_labels_eligible": False,
        "execution_time_equals_planning_budget": False,
        "dataset_diversity_or_split_independence_verified": False,
        "model_promotion_allowed": False,
        "authorizes_next_task": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("outer", "mission", "journal"):
        parser.add_argument(f"--{name}", type=Path, required=True)
        parser.add_argument(f"--{name}-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = admit(
        args.outer,
        args.outer_sha256,
        args.mission,
        args.mission_sha256,
        args.journal,
        args.journal_sha256,
    )
    result["evaluator_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps(result["labels"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
