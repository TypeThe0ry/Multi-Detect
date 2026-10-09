"""Current stage's retained-file integrity; no new SIM, control or inference."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / "docs/evidence/onboard-teacher-live-checkpoint-and-spawn-20261008-r1.json"
INDEX_KEY = "onboard_teacher_live_checkpoint_and_spawn_20261008_r1"


def _read(path):
    return json.loads(path.read_bytes())


def test_declared_files_and_index_bind_exact_current_bytes():
    stage = _read(STAGE)
    paths = set()
    for binding in (*stage["sources"], *stage["retained_artifact_bindings"]):
        path = (ROOT / binding["path"]).resolve(strict=True)
        assert path.is_relative_to(ROOT) and path not in paths
        paths.add(path)
        raw = path.read_bytes()
        assert len(raw) == binding["bytes"]
        assert hashlib.sha256(raw).hexdigest() == binding["sha256"]
    entry = _read(ROOT / "docs/current-progress.json")[INDEX_KEY]
    raw = STAGE.read_bytes()
    assert entry["evidence"] == {
        "path": STAGE.relative_to(ROOT).as_posix(),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    assert entry["claims"] == stage["claims"]
    assert entry["root_combined_CPU_tests"] == stage["validation"]["root_combined_CPU_tests"]


def test_actual_spawn_capture_is_ground_only_and_retains_failed_preflight():
    stage = _read(STAGE)
    capture = stage["validation"]["actual_capture"]
    original = _read(ROOT / capture["receipt_path"])
    raw = original["raw_telemetry"]
    assert capture["root_and_child_native_exit_codes"] == [0, 0]
    assert capture["packet_count"] == raw["packet_attempt_count"] == 5222
    assert raw["packet_attempt_count"] == raw["accepted_source_count"] + raw["foreign_source_count"]
    assert raw["orphan_packet_count"] == capture["orphan_packet_count"] == 0
    assert len(original["camera_frames"]) == capture["RGB_frames"] == capture["depth_frames"] == 4
    assert original["session"]["cleanup_verified"] and original["session"]["container_absent_after"]
    assert not original["receiver"]["queue_overflow"]
    assert original["receiver"]["received_packets"] == original["receiver"]["consumed_packets"]
    cameras = [_read(Path(frame["camera"]["path"])) for frame in original["camera_frames"]]
    assert len({camera["rgb_bgr24_sha256"] for camera in cameras}) == 1
    assert len({camera["provenance"]["depth_payload_sha256"] for camera in cameras}) == 1
    assert capture["motion_training_samples_created"] == 0
    assert capture["ARM_sent"] == capture["Mission_uploaded"] == capture["new_model_inference"] == 0
    failed = _read(ROOT / stage["validation"]["preflight_failure"]["receipt_path"])
    assert failed["child"]["native_exit_code"] == 1
    assert stage["validation"]["preflight_failure"]["Docker_started"] is False
    assert not stage["claims"]["exact_sensor_SDF_synchronization"]
    assert not stage["validation"]["actual_capture"][
        "source_inventory_complete_dependency_attestation"
    ]


def test_signed_geometry_compatibility_does_not_become_live_contact_or_route_permission():
    stage = _read(STAGE)
    geometry = _read(ROOT / stage["validation"]["signed_ground_geometry"]["receipt_path"])
    rows = geometry["results"]
    old = rows["retained_original_spawn_r1"]["audit"]
    new = rows["retained_raised_spawn_r2"]["audit"]
    assert old["predeclared_interval_m"] == new["predeclared_interval_m"] == [-1e-6, 1e-3]
    assert not old["geometry_compatible"] and new["geometry_compatible"]
    assert old["signed_bottom_minus_support_up_m"] < -0.06
    assert -1e-6 <= new["signed_bottom_minus_support_up_m"] < 0
    assert new["support_contains_conservative_footprint"]
    assert new["saved_body_up_tilt_deg"] <= new["maximum_ground_tilt_deg"]
    assert set(old["claims"].values()) == set(new["claims"].values()) == {False}
    assert stage["validation"]["signed_ground_geometry"]["independent_source_files_reopened"] == 18
    assert not stage["claims"]["current_live_ground_state_qualified"]
    assert not stage["claims"]["complete_route_clearance_passed"]


def test_live_checkpoints_and_v8_are_cpu_components_not_executed_authority():
    stage = _read(STAGE)
    live = stage["validation"]["live_checkpoint_component"]
    execution = stage["validation"]["V8_execution_component"]
    assert live["new_CPU_tests"] == 36 and execution["new_CPU_tests"] >= 49
    assert live["original_TTL_renewed"] is False
    assert live["source_wind_from_bound_SDF"] is True
    assert live["historical_runtime_wind_difference_retained"] is True
    assert execution["normal_ARM_param2"] == 0
    assert execution["existing_intent_maximum_s"] == 60
    assert execution["exact_transport_maximum_packets"] == 8192
    assert execution["real_original_draft_and_Grant_path_complete"] is False
    assert execution["complete_sortie_available"] is False
    assert set(stage["claims"].values()) == {False}


def test_release_default_reference_and_readme_preserve_the_evidence_boundary():
    stage = _read(STAGE)
    for binding in stage["protected_baseline_files"]:
        raw = (ROOT / binding["path"]).read_bytes()
        assert len(raw) == binding["bytes"]
        assert hashlib.sha256(raw).hexdigest() == binding["sha256"]
    assert stage["release_label"] == "0.3.0-pre.8"
    assert not stage["canonical_deployment_and_default_reference_modified"]
    readme = ROOT / "README.md"
    assert readme.stat().st_size < 50000
    assert "docs/onboard-teacher-live-checkpoint-and-spawn-20261008.md" in readme.read_text(
        encoding="utf-8"
    )
