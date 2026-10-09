import sys
from pathlib import Path

import pytest

from multidetect.clock import HighResolutionSystemClock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from scripts.vtol_sitl_mission_acceptance import (  # noqa: E402
    VtolMissionAcceptanceError,
    capture_clock_matches,
    validate_flight_capture_complete_marker,
)


@pytest.mark.parametrize("change", [None, "missing", "null", "session", "label", "resolution"])
def test_capture_complete_requires_caller_selected_identity(change):
    identity = HighResolutionSystemClock(session_id="barrier-parent").identity()
    payload = dict(
        schema="multidetect.vtol-sensor-capture-complete",
        schema_version=1,
        clock="host_perf_counter",
        host_clock_identity=dict(identity),
        capture_started_at_monotonic_s=101,
        capture_completed_at_monotonic_s=102,
        container_id="a" * 64,
        transaction_id="transaction",
        committed_protocol_sha256="b" * 64,
        scenario_id="scenario",
        scenario_manifest_hash="c" * 64,
        frame_count=1,
        sensor_binding_hash="d" * 64,
        frame_set_manifest_sha256="e" * 64,
        ground_truth_used_for_runtime_or_capture=False,
    )
    if change == "missing":
        del payload["host_clock_identity"]
    elif change == "null":
        payload["host_clock_identity"] = None
    elif change == "session":
        payload["host_clock_identity"]["session_id"] = "other"
    elif change == "resolution":
        payload["host_clock_identity"]["resolution_ns"] += 1
    elif change == "label":
        payload["clock"] = "host_monotonic"
    kwargs = dict(
        flight_ready_monotonic_s=100,
        container_id="a" * 64,
        transaction_id="transaction",
        committed_protocol_sha256="b" * 64,
        scenario_id="scenario",
        scenario_manifest_hash="c" * 64,
        host_clock_identity=identity,
    )
    if change is None:
        assert all(validate_flight_capture_complete_marker(payload, **kwargs).values())
    else:
        with pytest.raises(VtolMissionAcceptanceError):
            validate_flight_capture_complete_marker(payload, **kwargs)


@pytest.mark.parametrize("identity", [None, {}, {"source": "python.perf_counter"}])
def test_explicit_bad_identity_cannot_hide_behind_legacy_label(identity):
    assert not capture_clock_matches(
        {"clock": "host_monotonic", "host_clock_identity": identity}, label_key="clock"
    )


def test_legacy_absent_identity_remains_legacy():
    assert capture_clock_matches({"clock": "host_monotonic"}, label_key="clock")
