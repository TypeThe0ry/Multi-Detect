"""Recompute an owned-SIM campaign's second-task rejection from bound reports."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy import ContextSnapshot  # noqa: E402
from multidetect.autonomy.task_selection import select_next_task  # noqa: E402
from multidetect.px4_mission_feedback import (  # noqa: E402
    PX4_MISSION_FEEDBACK_CANONICAL_MAX_AGE_S,
)
from multidetect.simulation.energy_replay import CumulativeSimulationEnergy  # noqa: E402
from multidetect.simulator.flight_model import SimulationEnergyProfile  # noqa: E402
from scripts.vtol_selected_task_campaign import load_campaign_definition  # noqa: E402


def verify_energy(first, second):
    """Replay both prefixes through one fresh accumulator; never reset on landing."""
    energy = first["planning_energy"]
    session_id = first["container"]["id"]
    state = CumulativeSimulationEnergy(
        SimulationEnergyProfile(**energy["profile"]), session_id=session_id,
        initial_remaining_wh=energy["initial_remaining_wh"],
    )
    results = []
    for report in (first, second):
        supplied = report["planning_energy"]
        inputs = report["planning_energy_input"]
        history = inputs["history"]
        if history["receive_origin_host_monotonic_s"] != report["telemetry_session"][
            "collector_origin_host_monotonic_s"
        ]:
            raise ValueError("energy receive origin mismatch")
        replayed = state.observe(history, session_id=report["container"]["id"])
        if any(supplied[key] != value for key, value in replayed.items()):
            raise ValueError("energy replay differs from planning evidence")
        original = ContextSnapshot.from_mapping(inputs["input_context"])
        projected = replace(original, battery_remaining_pct=replayed["battery_remaining_pct"],
                            estimated_flight_time_s=replayed["estimated_flight_time_s"])
        if (original.canonical_hash != supplied["input_context_hash"]
                or projected.canonical_hash != supplied["projected_context_hash"]):
            raise ValueError("energy projection context hash differs")
        actual_hash = (report["selection_rejection"]["context_sha256"]
                       if report.get("selection_rejection") else
                       report["documents"]["context_snapshot"]["sha256"])
        if projected.canonical_hash != actual_hash:
            raise ValueError("planner did not consume projected energy context")
        latest = [s for s in history["samples"] if s["type"] == "GLOBAL_POSITION_INT"][-1]
        if not (0 <= original.captured_at_s - latest["at_s"]
                <= PX4_MISSION_FEEDBACK_CANONICAL_MAX_AGE_S):
            raise ValueError("energy observation is not fresh at context")
        results.append(replayed)
    return results


def verify(first, second, outer, candidates, *, require_energy=False, ranker=None):
    """Check report-level negative evidence, not a packet-capture proof."""
    def require(condition, reason):
        if not condition:
            raise ValueError(reason)

    first_context = ContextSnapshot.from_mapping(first["documents"]["context_snapshot"]["document"])
    require(first_context.canonical_hash == first["documents"]["context_snapshot"]["sha256"],
            "first context hash")
    initial = (select_next_task(candidates, first_context, "vtol").selected if ranker is None else
               ranker.select(candidates, first_context, "vtol"))
    require(initial is not None, "first task infeasible")
    task_id = first["documents"]["task_spec"]["document"]["task_id"]
    require(initial.candidate.task.task_id == task_id, "first task selection")
    outcome = first["shared_outcome_shadow"]["outcome"]
    require(first["gate_passed"] is True and outcome["status"] == "completed", "first completion")
    require(outcome["mission_hash"] == first["documents"]["executable_mission"]["sha256"],
            "first outcome hash")
    rejection = second["selection_rejection"]
    context = ContextSnapshot.from_mapping(rejection["context"])
    require(context.canonical_hash == rejection["context_sha256"], "rejection context hash")
    require(context.home_position == first_context.home_position, "home changed")
    require(context.captured_at_s > first_context.captured_at_s, "context did not advance")
    require(context.estimated_flight_time_s < first_context.estimated_flight_time_s,
            "endurance did not decrease")
    remaining = [item for item in candidates if item.task.task_id != task_id]
    require(bool(remaining), "no remaining candidates")
    decision = select_next_task(remaining, context, "vtol")
    require(decision.selected is None, "remaining task is feasible")
    reasons = [{"task_id": item.task_id, "reason": item.reason} for item in decision.rejected]
    require(reasons == rejection["rejected"], "rejection reasons differ")
    require(second["gate_passed"] is False, "rejected mission gate passed")
    require(second["transaction"]["final_state"] == "not_created"
            and second["transaction"]["state_events"] == [], "transaction created")
    require(second["mission_journal"]["exists"] is False
            and second["mission_journal"]["record_count"] == 0, "journal created")
    require(second["protocol"]["mission_item_count"] == 0
            and second["protocol"]["upload_acknowledged"] is False, "upload observed")
    require(second["mavlink"]["control_command_events"] == [], "control command observed")
    require(first["container"]["id"] == second["container"]["id"], "container changed")
    require(first["telemetry_session"]["collector_origin_host_monotonic_s"]
            == second["telemetry_session"]["collector_origin_host_monotonic_s"], "clock changed")
    cleanup = outer["container"]["cleanup"]
    require(cleanup["exact_id"] == first["container"]["id"]
            and cleanup["cleanup_succeeded"] is True
            and cleanup["post_remove_absence_proven"] is True, "cleanup unproven")
    energy = verify_energy(first, second) if require_energy else None
    return {"report_level_negative_verified": True, "rejected": reasons,
            "ranker_sha256": ranker.canonical_hash if ranker is not None else None,
            "energy_independently_replayed": require_energy, "energy_replay": energy,
            "first_task": task_id, "second_context_sha256": context.canonical_hash,
            "first_endurance_s": first_context.estimated_flight_time_s,
            "second_endurance_s": context.estimated_flight_time_s,
            "packet_capture_claimed": False, "physical_calibration_claimed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--run-directory", type=Path, required=True)
    parser.add_argument("--outer-report", type=Path, required=True)
    parser.add_argument("--require-energy", action="store_true")
    args = parser.parse_args()
    first_path = args.run_directory / "campaign-missions/mission-001.json"
    second_path = args.run_directory / "campaign-missions/mission-002.json"
    paths = [args.campaign, first_path, second_path, args.outer_report]
    raw = [path.read_bytes() for path in paths]
    first, second, outer = [json.loads(value) for value in raw[1:]]
    campaign_hash = hashlib.sha256(raw[0]).hexdigest()
    if campaign_hash != outer["campaign_input"]["sha256"]:
        raise ValueError("campaign input hash changed")
    candidates, _, ranker = load_campaign_definition(args.campaign)
    result = verify(first, second, outer, candidates, require_energy=args.require_energy,
                    ranker=ranker)
    journal = args.run_directory / "campaign-missions/mission-002.transaction.jsonl"
    if journal.exists():
        raise ValueError("second journal exists on disk")
    result["inputs"] = [{"path": str(path), "bytes": len(data),
                         "sha256": hashlib.sha256(data).hexdigest()}
                        for path, data in zip(paths, raw, strict=True)]
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
