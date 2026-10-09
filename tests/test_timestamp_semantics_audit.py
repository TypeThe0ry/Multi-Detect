import hashlib
import json

import pytest

from scripts.audit_timestamp_semantics import audit_file
from scripts.mission_dynamics_capture import MissionDynamicsCapture


def test_audit_marks_legacy_capture_without_promoting_it(tmp_path):
    capture = MissionDynamicsCapture().snapshot()
    del capture["timestamp_contract"]
    payload = {"flight": {"phase_dynamics_observations": capture}}
    raw = json.dumps(payload).encode()
    path = tmp_path / "legacy.json"
    path.write_bytes(raw)
    result = audit_file(path, hashlib.sha256(raw).hexdigest())
    assert result["timestamp_contract"]["status"] == (
        "legacy_capture_missing_explicit_timestamp_contract"
    )
    assert result["physical_synchrony_proven"] is False
    assert result["final_release_allowed"] is False


def test_audit_rejects_wrong_hash(tmp_path):
    path = tmp_path / "capture.json"
    path.write_text(
        json.dumps(
            {"flight": {"phase_dynamics_observations": MissionDynamicsCapture().snapshot()}}
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="SHA256"):
        audit_file(path, "0" * 64)
