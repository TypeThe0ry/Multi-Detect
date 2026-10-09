"""CLI source-consumption fixtures, not an external SIM flight."""

import json

import pytest
from test_sim_teacher_environment import _environment

from scripts.audit_teacher_environment_v1 import audit
from scripts.run_onboard_teacher_gazebo_observation_v2 import _json_new


def _input(tmp_path):
    environment = _environment(tmp_path)
    listing = {
        "contract": "multidetect.teacher-loaded-world-file-audit-v1",
        "status": "deliberately-not-trusted",
        "audit": {
            "world_source": environment.scene.world_source.to_mapping(),
            "generated_sdf_source": environment.generated_sdf_source.to_mapping(),
            "include_model_sources": {},
            "collision_definitions_match": False,
        },
    }
    _json_new(tmp_path / "listing.json", listing)
    return tmp_path / "listing.json"


def test_cli_recomputes_sources_not_status_or_claimed_pass(tmp_path):
    source = _input(tmp_path)
    output = tmp_path / "artifacts/diagnostics/audit"
    assert audit(source, output, [(0, 0), (20, 15)], artifact_root=tmp_path) == 0
    report = json.loads((output / "environment-audit.json").read_text())
    assert report["status"] == "retained-environment-recomputed"
    assert report["surface_queries"][0]["highest_static_surface_bound_up_m"] == 0.08
    assert set(report["claims"].values()) == {False}
    with pytest.raises(ValueError, match="fresh"):
        audit(source, output, [(0, 0)], artifact_root=tmp_path)


def test_cli_retains_failure_when_original_geometry_drifts(tmp_path):
    source = _input(tmp_path)
    (tmp_path / "loaded.bin").write_bytes(b"changed")
    output = tmp_path / "artifacts/diagnostics/refused"
    assert audit(source, output, [(0, 0)], artifact_root=tmp_path) == 1
    report = json.loads((output / "environment-audit.json").read_text())
    assert report["status"] == "environment-audit-refused"
    assert report["environment"] is None
    assert set(report["claims"].values()) == {False}


def test_cli_rejects_output_escape_and_uncovered_query(tmp_path):
    source = _input(tmp_path)
    with pytest.raises(ValueError, match="contained"):
        audit(source, tmp_path / "elsewhere", [(0, 0)], artifact_root=tmp_path)
    output = tmp_path / "artifacts/diagnostics/outside"
    assert audit(source, output, [(500, 0)], artifact_root=tmp_path) == 1
    assert "covers" in json.loads((output / "environment-audit.json").read_text())["error"]
