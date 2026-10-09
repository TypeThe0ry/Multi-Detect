"""Compare current planning estimates with hash-bound historical SIM observations.

Clock domains and conservative budget semantics are not silently equated.
This read-only audit does not recalibrate a model or reauthorize execution.
"""

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

from multidetect.autonomy import ContextSnapshot, ResolvedTaskSpec  # noqa: E402
from multidetect.autonomy.planner import estimate_task, plan_task  # noqa: E402
from multidetect.autonomy.serialization import canonical_sha256, strict_json_loads  # noqa: E402
from multidetect.simulator.flight_model import SimulationEnergyProfile  # noqa: E402
from scripts.audit_mission_energy_window import audit_window  # noqa: E402


def _number(value):
    if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
        raise ValueError("finite nonnegative time required")
    return value


def audit_report(report):
    if report.get("gate_passed") is not True or report.get("failure") is not None:
        raise ValueError("completed successful report required")
    docs = report["documents"]
    for name in ("task_spec", "context_snapshot", "platform_neutral_plan", "executable_mission"):
        if canonical_sha256(docs[name]["document"]) != docs[name]["sha256"]:
            raise ValueError(f"{name} document hash mismatch")
    task = ResolvedTaskSpec.from_mapping(docs["task_spec"]["document"])
    context = ContextSnapshot.from_mapping(docs["context_snapshot"]["document"])
    vehicle = docs["mission_dispatcher"]["vehicle_class_normalized"]
    plan = plan_task(task, context, vehicle)
    if plan.canonical_hash != docs["platform_neutral_plan"]["sha256"]:
        raise ValueError("current plan differs from historical plan")
    mission = docs["executable_mission"]["document"]
    if mission["platform_plan_hash"] != plan.canonical_hash:
        raise ValueError("mission plan binding mismatch")
    outcome = report["shared_outcome_shadow"]["outcome"]
    if (
        outcome["status"] != "completed"
        or outcome["mission_hash"] != docs["executable_mission"]["sha256"]
    ):
        raise ValueError("completed outcome mission binding mismatch")
    flight = report["flight"]
    observer = _number(flight["observer_origin_host_monotonic_s"])
    end = _number(flight["completed_host_monotonic_s"])
    duration = _number(flight["duration_s"])
    if not math.isclose(end - observer, duration, abs_tol=1e-5, rel_tol=0):
        raise ValueError("observer duration mismatch")
    if not math.isclose(_number(outcome["elapsed_s"]), duration, abs_tol=1e-5, rel_tol=0):
        raise ValueError("outcome duration mismatch")
    window = report["mission_receive_window"]
    arm = _number(window["arm_request_host_monotonic_s"])
    if not arm <= observer < end or window["completed_host_monotonic_s"] != end:
        raise ValueError("mission clock ordering mismatch")
    p = report["planning_energy"]["profile"]
    profile = SimulationEnergyProfile(
        p["capacity_wh"], p["avionics_power_w"], p["propulsion_max_power_w"], p["provenance"]
    )
    energy = audit_window(report, profile)
    clock_comparison = compare_clock_windows(energy, end - arm)
    estimate = estimate_task(task, context, vehicle)
    return {
        "schema_version": 1,
        "task_id": task.task_id,
        "context_sha256": context.canonical_hash,
        "plan_sha256": plan.canonical_hash,
        "mission_sha256": docs["executable_mission"]["sha256"],
        "current_planning_estimate": estimate.to_mapping(),
        "observed_host_receive": {
            "arm_to_completed_s": end - arm,
            "observer_to_completed_s": duration,
            "arm_to_observer_s": observer - arm,
        },
        "energy_window_audit": energy,
        "clock_window_comparison": clock_comparison,
        "profile": p,
        "clock_domains_equated": False,
        "duration_bias_or_calibration_claimed": False,
        "physical_battery_measurement_claimed": False,
        "journal_revalidated": False,
        "executable_recompiled": False,
        "authorizes_next_task": False,
    }


def compare_clock_windows(energy, host_duration_s):
    """Describe receive-attributed boot intervals, not synchronized flight time."""
    host = _number(host_duration_s)
    if host <= 0:
        raise ValueError("positive host window required")
    result = {
        "method": "receive_attributed_boot_interval_envelope_v1",
        "host_receive_duration_s": host,
        "boot_interval_envelope_s": None,
        "boot_seconds_per_host_second": None,
        "clock_synchronization_claimed": False,
        "gazebo_realtime_factor_claimed": False,
        "calibration_eligible": False,
    }
    envelope = energy["window_energy_envelope"]
    if not energy["receive_window_bracketed_by_valid_energy_prefix"] or envelope is None:
        result["reason"] = "incomplete_energy_prefix_window_coverage"
        return result
    lower = _number(envelope["strict_interior_boot_s"])
    boundary = _number(envelope["boundary_boot_s"])
    upper = _number(lower + boundary)
    ratios = {"lower": _number(lower / host), "upper": _number(upper / host)}
    result.update(
        reason="complete_receive_attributed_interval_envelope",
        boot_interval_envelope_s={"lower": lower, "upper": upper, "boundary": boundary},
        boot_seconds_per_host_second=ratios,
    )
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--report-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise ValueError("output must be new")
    with args.report.open("rb") as stream:
        raw = stream.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024:
        raise ValueError("report exceeds 32 MiB")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != args.report_sha256:
        raise ValueError("report byte hash mismatch")
    result = audit_report(strict_json_loads(raw.decode("utf-8")))
    result["input"] = {"path": str(args.report), "bytes": len(raw), "sha256": digest}
    result["evaluator_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
        stream.write("\n")
    print(
        json.dumps(
            {
                "task_id": result["task_id"],
                "estimated_s": result["current_planning_estimate"]["estimated_duration_s"],
                "host_receive": result["observed_host_receive"],
                "energy_bracketed": result["energy_window_audit"][
                    "receive_window_bracketed_by_valid_energy_prefix"
                ],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
