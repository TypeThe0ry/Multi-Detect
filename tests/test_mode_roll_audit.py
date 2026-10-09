import hashlib
import json

import pytest
from test_mode_window import event
from test_roll_response import capture

from scripts.audit_mode_roll_response import audit_file


def audit(path, digest, **extra):
    return audit_file(
        path,
        digest,
        lookbacks_ms=[500, 1000],
        maximum_gap_ms=300,
        maximum_target_deviation_deg=2,
        quaternion_norm_tolerance=1e-5,
        required_state=4,
        maximum_gap_s=0.5,
        **extra,
    )


def test_hash_binding_and_empty_mode_coverage(tmp_path):
    path = tmp_path / "report.json"
    raw = json.dumps(
        dict(
            flight=dict(phase_dynamics_observations=capture()),
            shared_outcome_shadow=dict(events=[]),
        )
    ).encode()
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    with pytest.raises(ValueError, match="SHA256"):
        audit(path, "0" * 64)
    result = audit(path, digest)
    assert all(w["retained_count"] == 0 for w in result["windows"])
    assert result["windows"][0]["groups"]["sampled_near_current_target"]["rms_error_deg"] is None
    assert result["final_release_allowed"] is False


def test_mode_filter_keeps_only_bracketed_rows(tmp_path):
    data = capture((0, 0, 0, 0, 0, 0))
    raw = json.dumps(
        dict(
            flight=dict(phase_dynamics_observations=data),
            shared_outcome_shadow=dict(events=[event(i / 4) for i in range(7)]),
        )
    ).encode()
    path = tmp_path / "report.json"
    path.write_bytes(raw)
    result = audit(path, hashlib.sha256(raw).hexdigest())
    assert [w["retained_count"] for w in result["windows"]] == [3, 1]
    assert result["windows"][0]["groups"]["sampled_near_current_target"]["count"] == 3


def test_target_coverage_finds_quiet_targets_missing_from_exact_pairs(tmp_path):
    data = capture((0, 0, 0, 0, 0, 0))
    for row in data["streams"]["ATTITUDE"]["samples"]:
        row["time_boot_ms"] += 1
    raw = json.dumps(
        dict(
            flight=dict(phase_dynamics_observations=data),
            shared_outcome_shadow=dict(events=[event(i / 4) for i in range(7)]),
        )
    ).encode()
    path = tmp_path / "report.json"
    path.write_bytes(raw)
    result = audit(path, hashlib.sha256(raw).hexdigest(), include_target_coverage=True)
    window = result["windows"][0]
    assert window["retained_count"] == 0
    assert window["target_coverage"]["retained_targets"] == 3
    assert window["target_coverage"]["groups"]["sampled_near_current_target"] == {
        "exact_label_present": 0,
        "exact_label_absent": 3,
    }
