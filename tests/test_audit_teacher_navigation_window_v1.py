"""CPU CLI refusal fixtures; not an actual navigation-window validation."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from scripts import audit_teacher_navigation_window_v1 as cli


def _fixture(monkeypatch, tmp_path, *, result=None, drift=False):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    extraction = tmp_path / "extraction.json"
    monkeypatch.setattr(cli, "EXTRACTION", extraction)
    assets = {}
    for key in ("x500-model.sdf", "x500-base-model.sdf"):
        path = tmp_path / key
        path.write_bytes(b"CPU model placeholder")
        source = cli.binding(path)
        assets[key] = {
            "source": {"path": str(path), "bytes": source.bytes, "sha256": source.sha256}
        }
    extraction.write_text(
        json.dumps(
            {
                "status": "original-image-assets-recorded",
                "image": "CPU-image",
                "never_started_identity_verified": True,
                "owned_container_absent_after_cleanup": True,
                "injected_native_used": False,
                "assets": assets,
            }
        )
    )
    for relative in (
        "simulation/gazebo/models/x500_mono_cam/model.sdf",
        "simulation/gazebo/worlds/urban_inspection_multirotor_001.sdf",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"CPU SDF placeholder")
    capture = tmp_path / "capture.json"
    capture.write_text(
        json.dumps(
            {"plan": {"docker_plan": {"image": "CPU-image"}}, "clock": {"session_id": "CPU-clock"}}
        )
    )
    output = tmp_path / "artifacts/diagnostics/audit"
    output.parent.mkdir(parents=True)
    if result is None:
        result = {
            "origin_qualified": False,
            "derived_origin": None,
            "window_coordinates_verified": True,
        }

    def fake_audit(**kwargs):
        assert kwargs["artifact_root"] == tmp_path
        assert kwargs["clock_id"] == "CPU-clock"
        assert set(kwargs["model_sources"]) == {"x500", "x500_base", "x500_mono_cam"}
        if drift:
            capture.write_text("{}")
        return SimpleNamespace(to_mapping=lambda: result)

    monkeypatch.setattr(cli.navref, "audit_sim_navigation_window", fake_audit, raising=False)
    return capture, output


def test_retained_result_records_no_live_authority(monkeypatch, tmp_path):
    capture, output = _fixture(monkeypatch, tmp_path)
    assert cli.audit(capture, output) == 0
    report = json.loads((output / "navigation-window-audit.json").read_bytes())
    assert report["status"] == "retained-window-coordinates-verified"
    assert set(report["claims"].values()) == {False}


@pytest.mark.parametrize(
    "result",
    [
        {"origin_qualified": True, "derived_origin": None, "window_coordinates_verified": True},
        {"origin_qualified": False, "derived_origin": {}, "window_coordinates_verified": True},
        {"origin_qualified": False, "derived_origin": None, "window_coordinates_verified": False},
    ],
)
def test_active_or_unverified_output_is_refused(monkeypatch, tmp_path, result):
    capture, output = _fixture(monkeypatch, tmp_path, result=result)
    assert cli.audit(capture, output) == 1
    report = json.loads((output / "navigation-window-audit.json").read_bytes())
    assert report["error"]
    assert report["audit"] is None


def test_capture_drift_not_hidden_by_success(monkeypatch, tmp_path):
    capture, output = _fixture(monkeypatch, tmp_path, drift=True)
    assert cli.audit(capture, output) == 1
    report = json.loads((output / "navigation-window-audit.json").read_bytes())
    assert "changed" in report["error"]


def test_wrong_extraction_image_refused(monkeypatch, tmp_path):
    capture, output = _fixture(monkeypatch, tmp_path)
    source = json.loads(cli.EXTRACTION.read_bytes())
    source["image"] = "different-CPU-image"
    cli.EXTRACTION.write_text(json.dumps(source))
    assert cli.audit(capture, output) == 1
    assert (
        "does not match"
        in json.loads((output / "navigation-window-audit.json").read_bytes())["error"]
    )


def test_output_not_overwritten(monkeypatch, tmp_path):
    capture, output = _fixture(monkeypatch, tmp_path)
    output.mkdir()
    with pytest.raises(ValueError, match="fresh"):
        cli.audit(capture, output)
