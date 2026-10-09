import importlib.util
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from multidetect.simulator.flight_model import SimulationEnergyProfile

SPEC = importlib.util.spec_from_file_location(
    "energy_window_audit",
    Path(__file__).resolve().parents[1] / "scripts/audit_mission_energy_window.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
PROFILE = SimulationEnergyProfile(10, 36, 324, "fixture only")


def report():
    rows = []
    for stamp in (0.0, 1.0, 2.0):
        for kind, field, value in (
            ("HEARTBEAT", "base_mode", 128),
            ("VFR_HUD", "throttle", 10),
            ("GLOBAL_POSITION_INT", "time_boot_ms", int(1000 + stamp * 1000)),
        ):
            rows.append(
                dict(
                    type=kind,
                    field=field,
                    value=value,
                    at_s=stamp,
                    source_system=1,
                    source_component=1,
                )
            )
    clock = "host_monotonic_since_collector_start"
    return {
        "flight": {
            "post_mission_observation": {
                "energy_input_history": {
                    "samples": rows,
                    "samples_seen": len(rows),
                    "samples_dropped": 0,
                    "receive_origin_host_monotonic_s": 100,
                    "receive_clock": clock,
                }
            }
        },
        "mission_receive_window": {
            "receive_origin_host_monotonic_s": 100,
            "clock": clock,
            "arm_request_host_monotonic_s": 100.2,
            "completed_host_monotonic_s": 101.8,
        },
    }


def test_brackets_receive_window_emits_model_window_energy_without_physical_claim():
    data = report()
    original = deepcopy(data)
    result = MODULE.audit_window(data, PROFILE)
    assert result["receive_window_bracketed_by_valid_energy_prefix"] is True
    assert result["left_sample"]["at_s"] == 0
    assert result["right_sample"]["at_s"] == 2
    assert result["window_energy_wh"] == pytest.approx(
        (0 + 68.4 * 2 / 3600) / 2
    )
    assert result["window_energy_method"] == "causal_envelope_midpoint_model_v1"
    envelope = result["window_energy_envelope"]
    assert envelope["lower_wh"] == 0
    assert envelope["upper_wh"] == pytest.approx(68.4 * 2 / 3600)
    assert envelope["boundary_intervals"] == 2
    assert envelope["physical_error_bound_claimed"] is False
    assert result["full_mission_energy_claimed"] is True
    assert result["full_mission_energy_semantics"] == "complete_receive_window_model_only"
    assert result["authorizes_next_task"] is False
    assert data == original


def test_late_endpoint_is_not_filled():
    data = report()
    data["mission_receive_window"]["completed_host_monotonic_s"] = 102.1
    result = MODULE.audit_window(data, PROFILE)
    assert result["missing_boundaries"] == ["end"]
    assert result["window_energy_envelope"] is None


def test_stale_interval_cannot_bracket_from_later_sample():
    data = report()
    rows = data["flight"]["post_mission_observation"]["energy_input_history"]["samples"]
    rows[-1]["at_s"] = 4
    result = MODULE.audit_window(data, PROFILE)
    assert result["missing_boundaries"] == ["end"]
    assert result["replay"]["reason"] == "load_or_receive_gap"


@pytest.mark.parametrize(
    "key,value",
    [
        ("receive_origin_host_monotonic_s", 99),
        ("arm_request_host_monotonic_s", True),
        ("completed_host_monotonic_s", float("nan")),
        ("completed_host_monotonic_s", 99),
    ],
)
def test_invalid_origin_or_window_rejected(key, value):
    data = report()
    data["mission_receive_window"][key] = value
    with pytest.raises(ValueError):
        MODULE.audit_window(data, PROFILE)


def test_interior_energy_uses_left_load_and_boot_dt():
    data = report()
    history = data["flight"]["post_mission_observation"]["energy_input_history"]
    # Four positions at host 0, .5, 1, 1.5; boot increments stay one second.
    rows = history["samples"]
    more = deepcopy(rows[-3:])
    for row in more:
        row["at_s"] = 3
        if row["type"] == "GLOBAL_POSITION_INT":
            row["value"] = 4000
    rows.extend(more)
    for row in rows:
        row["at_s"] /= 2
        if row["at_s"] >= 1 and row["type"] == "VFR_HUD":
            row["value"] = 100
    history["samples_seen"] = len(rows)
    data["mission_receive_window"]["completed_host_monotonic_s"] = 101.3
    envelope = MODULE.audit_window(data, PROFILE)["window_energy_envelope"]
    assert envelope["strict_interior_intervals"] == 1
    assert envelope["strict_interior_boot_s"] == 1
    assert envelope["lower_wh"] == pytest.approx(68.4 / 3600)
    assert envelope["boundary_uncertainty_wh"] == pytest.approx((68.4 + 360) / 3600)


def test_exact_receive_boundary_is_conservatively_ambiguous():
    data = report()
    data["mission_receive_window"]["arm_request_host_monotonic_s"] = 100
    data["mission_receive_window"]["completed_host_monotonic_s"] = 102
    envelope = MODULE.audit_window(data, PROFILE)["window_energy_envelope"]
    assert envelope["strict_interior_intervals"] == 0
    assert envelope["boundary_intervals"] == 2
    assert envelope["boundary_boot_s"] == 2


def clock_producers(data):
    flight = data["flight"]
    observation = flight["post_mission_observation"]
    return (
        data["mission_receive_window"],
        flight,
        observation,
        observation["energy_input_history"],
    )


def qpc_report():
    data = report()
    identity = dict(
        schema_version=1,
        source="python.perf_counter",
        session_id="fixture-session",
        implementation="fixture QPC",
        resolution_ns=100,
        accuracy_verified=False,
        legacy_monotonic_equivalent=False,
    )
    for producer in clock_producers(data):
        producer["host_clock_identity"] = deepcopy(identity)
    window, _, _, history = clock_producers(data)
    window["clock"] = history["receive_clock"] = "host_perf_counter_since_collector_start"
    return data


def test_qpc_bound_window_preserves_canonical_energy_and_input():
    data = qpc_report()
    original = deepcopy(data)
    result = MODULE.audit_window(data, PROFILE)
    legacy = MODULE.audit_window(report(), PROFILE)
    assert result["replay"] == legacy["replay"]
    assert result["window_energy_envelope"] == legacy["window_energy_envelope"]
    assert result["host_clock_identity"] == data["mission_receive_window"]["host_clock_identity"]
    result["host_clock_identity"]["session_id"] = "not-aliased"
    assert data == original


@pytest.mark.parametrize("producer_index", range(4))
@pytest.mark.parametrize(
    "change", ["missing", "null", "session", "implementation", "resolution", "source"]
)
def test_qpc_rejects_unbound_or_cross_domain_producers(producer_index, change):
    data = qpc_report()
    producer = clock_producers(data)[producer_index]
    if change == "missing":
        del producer["host_clock_identity"]
    elif change == "null":
        producer["host_clock_identity"] = None
    else:
        key, value = {
            "session": ("session_id", "different-session"),
            "implementation": ("implementation", "different-platform"),
            "resolution": ("resolution_ns", 200),
            "source": ("source", "python.monotonic"),
        }[change]
        producer["host_clock_identity"][key] = value
    with pytest.raises(ValueError):
        MODULE.audit_window(data, PROFILE)


@pytest.mark.parametrize("producer_index", range(4))
def test_legacy_label_cannot_hide_qpc_identity(producer_index):
    data = report()
    clock_producers(data)[producer_index]["host_clock_identity"] = qpc_report()[
        "mission_receive_window"
    ]["host_clock_identity"]
    with pytest.raises(ValueError, match="legacy"):
        MODULE.audit_window(data, PROFILE)


def test_explicit_legacy_identity_supported():
    data = report()
    for producer in clock_producers(data):
        producer["host_clock_identity"] = {"source": "python.monotonic", "session_id": None}
    assert MODULE.audit_window(data, PROFILE) == MODULE.audit_window(report(), PROFILE)


def test_qpc_history_label_must_match_window():
    data = qpc_report()
    clock_producers(data)[-1]["receive_clock"] = "host_monotonic_since_collector_start"
    with pytest.raises(ValueError, match="label"):
        MODULE.audit_window(data, PROFILE)


def test_direct_cli_bootstraps_imports_outside_checkout(tmp_path):
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, str(Path(MODULE.__file__).resolve()), "--help"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "--capacity-wh" in result.stdout
