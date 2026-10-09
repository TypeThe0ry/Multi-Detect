import hashlib
import json
from pathlib import Path

import pytest

from multidetect.autonomy.planner import plan_task
from multidetect.autonomy.serialization import canonical_sha256
from scripts.audit_planning_execution_gap import audit_report, compare_clock_windows, main
from scripts.train_task_ranker import load_dataset


def fixture_report():
    path = Path(__file__).resolve().parents[1] / "configs/training/task-ranker-budget-v1.json"
    groups, _, _ = load_dataset(path)
    case = groups["train"][0]
    task = case.candidates[0].task
    plan = plan_task(task, case.context, case.vehicle_class)
    values = {
        "task_spec": task.to_mapping(),
        "context_snapshot": case.context.to_mapping(),
        "platform_neutral_plan": plan.to_mapping(),
        "executable_mission": {"platform_plan_hash": plan.canonical_hash},
    }
    docs = {
        key: {"document": value, "sha256": canonical_sha256(value)} for key, value in values.items()
    }
    docs["mission_dispatcher"] = {"vehicle_class_normalized": "vtol"}
    rows = [
        {
            "type": kind,
            "field": field,
            "value": value,
            "at_s": stamp,
            "source_system": 1,
            "source_component": 1,
        }
        for stamp in (0.0, 1.0, 2.0)
        for kind, field, value in (
            ("HEARTBEAT", "base_mode", 128),
            ("VFR_HUD", "throttle", 10),
            ("GLOBAL_POSITION_INT", "time_boot_ms", int(1000 + stamp * 1000)),
        )
    ]
    clock = "host_monotonic_since_collector_start"
    return {
        "gate_passed": True,
        "failure": None,
        "documents": docs,
        "shared_outcome_shadow": {
            "outcome": {
                "status": "completed",
                "mission_hash": docs["executable_mission"]["sha256"],
                "elapsed_s": 1.5,
            }
        },
        "flight": {
            "observer_origin_host_monotonic_s": 100.3,
            "completed_host_monotonic_s": 101.8,
            "duration_s": 1.5,
            "post_mission_observation": {
                "energy_input_history": {
                    "samples": rows,
                    "samples_seen": len(rows),
                    "samples_dropped": 0,
                    "receive_origin_host_monotonic_s": 100,
                    "receive_clock": clock,
                }
            },
        },
        "mission_receive_window": {
            "receive_origin_host_monotonic_s": 100,
            "clock": clock,
            "arm_request_host_monotonic_s": 100.2,
            "completed_host_monotonic_s": 101.8,
        },
        "planning_energy": {
            "profile": {
                "capacity_wh": 10,
                "avionics_power_w": 36,
                "propulsion_max_power_w": 324,
                "provenance": "synthetic fixture",
            }
        },
    }


def test_bound_comparison_retains_clock_and_calibration_limits():
    result = audit_report(fixture_report())
    assert result["observed_host_receive"]["arm_to_completed_s"] == pytest.approx(1.6)
    assert result["energy_window_audit"]["receive_window_bracketed_by_valid_energy_prefix"]
    assert result["current_planning_estimate"]["estimated_duration_s"] > 0
    assert not result["clock_domains_equated"]
    assert not result["duration_bias_or_calibration_claimed"]
    comparison = result["clock_window_comparison"]
    assert comparison["boot_interval_envelope_s"] == {"lower": 0, "upper": 2, "boundary": 2}
    assert comparison["boot_seconds_per_host_second"]["upper"] == pytest.approx(1.25)
    assert not comparison["calibration_eligible"]


def test_clock_comparison_missing_boundary_does_not_extrapolate():
    result = compare_clock_windows(
        {"receive_window_bracketed_by_valid_energy_prefix": False, "window_energy_envelope": None},
        10,
    )
    assert result["boot_interval_envelope_s"] is None
    assert result["boot_seconds_per_host_second"] is None
    assert not result["calibration_eligible"]


@pytest.mark.parametrize("value", [0, -1, True, float("nan"), float("inf")])
def test_clock_comparison_rejects_bad_host_duration(value):
    with pytest.raises(ValueError):
        compare_clock_windows({}, value)


def test_clock_comparison_preserves_slow_boot_scale_without_clamping():
    result = compare_clock_windows(
        {
            "receive_window_bracketed_by_valid_energy_prefix": True,
            "window_energy_envelope": {"strict_interior_boot_s": 7, "boundary_boot_s": 1},
        },
        20,
    )
    assert result["boot_seconds_per_host_second"] == {"lower": 0.35, "upper": 0.4}
    assert not result["gazebo_realtime_factor_claimed"]


@pytest.mark.parametrize("fault", ["hash", "outcome", "duration", "nan", "clock", "failed"])
def test_invalid_evidence_rejected(fault):
    data = fixture_report()
    if fault == "hash":
        data["documents"]["task_spec"]["sha256"] = "0" * 64
    elif fault == "outcome":
        data["shared_outcome_shadow"]["outcome"]["mission_hash"] = "0" * 64
    elif fault == "duration":
        data["flight"]["duration_s"] = 2
    elif fault == "nan":
        data["flight"]["observer_origin_host_monotonic_s"] = float("nan")
    elif fault == "clock":
        data["mission_receive_window"]["receive_origin_host_monotonic_s"] = 99
    else:
        data["gate_passed"] = False
    with pytest.raises(ValueError):
        audit_report(data)


def test_cli_binds_bytes_and_preserves_previous_output(tmp_path):
    source, output = tmp_path / "report.json", tmp_path / "audit.json"
    source.write_text(json.dumps(fixture_report()), encoding="utf-8")
    args = ["--report", str(source), "--output", str(output), "--report-sha256"]
    with pytest.raises(ValueError, match="byte hash"):
        main(args + ["0" * 64])
    assert not output.exists()
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    assert main(args + [digest]) == 0
    raw = output.read_bytes()
    with pytest.raises(ValueError, match="new"):
        main(args + [digest])
    assert output.read_bytes() == raw
