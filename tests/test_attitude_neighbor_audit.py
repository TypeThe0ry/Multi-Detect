import hashlib
import json

import pytest
from test_mode_window import event
from test_roll_response import capture

from scripts.audit_attitude_neighbor_window import audit_file


def test_hash_binding_and_no_target_window(tmp_path):
    data = capture()
    payload = {
        "flight": {"phase_dynamics_observations": data},
        "shared_outcome_shadow": {"events": []},
    }
    raw = json.dumps(payload).encode()
    path = tmp_path / "report.json"
    path.write_bytes(raw)
    kwargs = dict(
        lookback_ms=500,
        maximum_gap_ms=300,
        maximum_target_deviation_deg=2,
        quaternion_norm_tolerance=1e-5,
        required_state=4,
        maximum_gap_s=1.5,
    )
    with pytest.raises(ValueError, match="SHA256"):
        audit_file(path, "0" * 64, **kwargs)
    result = audit_file(path, hashlib.sha256(raw).hexdigest(), **kwargs)
    assert result["quiet_unpaired_count"] == 0
    assert result["interpolation_performed"] is False


def test_unpaired_quiet_target_endpoint_report(tmp_path):
    data = capture((0, 0, 0, 0, 0, 0))
    for row in data["streams"]["ATTITUDE"]["samples"]:
        row["time_boot_ms"] += 1
    for row in data["streams"]["ATTITUDE_TARGET"]["samples"]:
        row["received_at_s"] += 0.1
    payload = {
        "flight": {"phase_dynamics_observations": data},
        "shared_outcome_shadow": {"events": [event(i / 4) for i in range(7)]},
    }
    raw = json.dumps(payload).encode()
    path = tmp_path / "report.json"
    path.write_bytes(raw)
    result = audit_file(
        path,
        hashlib.sha256(raw).hexdigest(),
        lookback_ms=500,
        maximum_gap_ms=300,
        maximum_target_deviation_deg=2,
        quaternion_norm_tolerance=1e-5,
        required_state=4,
        maximum_gap_s=0.5,
    )
    assert result["quiet_unpaired_count"] == 4
    assert result["summary"]["bracket_width_ms_min"] == 250
    assert result["summary"]["observed_endpoint_roll_change_abs_max_deg"] == 0
