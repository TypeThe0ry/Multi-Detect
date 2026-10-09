"""Retained-file stage/index checks, not a new external SIM or flight run."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / "docs/evidence/onboard-teacher-origin-window-and-normal-mission-20261008-r1.json"


def _stage():
    return json.loads(STAGE.read_bytes())


def test_every_declared_stage_file_has_exact_current_bytes_and_hash():
    document = _stage()
    for binding in (*document["sources"], *document["retained_artifact_bindings"]):
        path = ROOT / binding["path"]
        assert path.resolve().is_relative_to(ROOT)
        raw = path.read_bytes()
        assert len(raw) == binding["bytes"]
        assert hashlib.sha256(raw).hexdigest() == binding["sha256"]


def test_progress_index_binds_new_stage_without_claiming_flight():
    progress = json.loads((ROOT / "docs/current-progress.json").read_bytes())
    entry = progress["onboard_teacher_origin_window_and_normal_Mission_20261008_r1"]
    assert entry["evidence"]["path"] == STAGE.relative_to(ROOT).as_posix()
    raw = STAGE.read_bytes()
    assert len(raw) == entry["evidence"]["bytes"]
    assert hashlib.sha256(raw).hexdigest() == entry["evidence"]["sha256"]
    assert set(entry["claims"].values()) == {False}
    assert entry["root_combined_CPU_tests"] == 602


def test_actual_run_and_retained_audits_remain_separate_from_live_origin():
    stage = _stage()
    capture = stage["validation"]["actual_capture"]
    assert capture["root_and_child_native_exit_codes"] == [0, 0]
    assert (
        capture["packet_count"]
        == capture["accepted_source_count"] + capture["foreign_source_count"]
    )
    assert capture["orphan_packet_count"] == 0
    assert capture["new_ARM_commands"] == capture["new_Mission_uploads"] == 0
    assert not capture["source_inventory_complete"]
    audit = stage["validation"]["navigation_window_audit"]
    assert audit["first_attempt_native_exit_code"] == 1
    assert audit["scoped_recomputation_native_exit_code"] == 0
    assert audit["window_coordinates_verified"]
    assert not audit["origin_qualified"] and audit["derived_origin"] is None
    assert audit["GPS_GLOBAL_ORIGIN_window_records"] == 0
    assert audit["GPS_GLOBAL_ORIGIN_original_received_at_s"] < capture["host_camera_bracket_s"][0]
    assert not audit["timestamp_renewed"]
    assert not audit["physical_accuracy_acceptance"]


def test_normal_arm_is_only_CPU_verified_and_does_not_create_permission():
    stage = _stage()
    component = stage["validation"]["normal_execution_component"]
    assert component["normal_ARM_command"] == 400
    assert component["normal_ARM_param2"] == 0
    assert not component["force_ARM_used"]
    assert not component["creates_Grant_or_PASS"]
    for key in (
        "ARM_sent",
        "actual_airborne_flight",
        "optimizer_updates",
        "candidate_promoted",
        "hil_real_evidence",
        "jetson_runtime_proven",
        "outdoor_gnss_evidence",
        "final_release_allowed",
    ):
        assert stage["claims"][key] is False


def test_readme_links_current_stage_and_release_remains_prerelease():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/onboard-teacher-origin-window-and-normal-mission-20261008.md" in text
    assert (ROOT / "README.md").stat().st_size < 50000
    assert _stage()["release_label"] == "0.3.0-pre.8"
    assert not _stage()["canonical_deployment_and_default_reference_modified"]
