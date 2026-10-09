from __future__ import annotations

import copy
import hashlib
import json
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest
from test_train_onboard_model import _trajectory_document

from multidetect.autonomy.contracts import ContextSnapshot
from multidetect.autonomy.onboard_agent import OnboardModelInput
from multidetect.backends.raw_replay import RawSensorReplayRecording
from scripts import audit_onboard_observation_corpus as audit


def _hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def _refresh_row(row: dict) -> None:
    context = ContextSnapshot.from_mapping(row["observation"]["context"])
    event = audit._event(row["observation"]["event"])
    row["features"] = list(OnboardModelInput(context, event).feature_vector())
    row["source"].update(context_sha256=context.canonical_hash, event_sha256=event.canonical_hash)


def _fixture(tmp_path: Path, count: int = 8) -> Path:
    """Tiny native PNG/raw-recording fixture, never evidence of a real SIM run."""
    cv2 = pytest.importorskip("cv2")
    tmp_path.mkdir(parents=True, exist_ok=True)
    document = _trajectory_document()
    document.update(
        schema="multidetect.onboard-trajectory-staging",
        contract="multidetect.onboard-trajectory-staging-v1",
        training_ready=False,
        missing_context_fields=[],
    )
    episode = document["episodes"][0]
    episode["episode_id"] = "fixture-observation"
    template = copy.deepcopy(episode["transitions"][0])
    frames, depth_frames, rows, events = [], [], [], []
    origin = datetime(2026, 10, 5, tzinfo=timezone.utc)
    for index in range(count):
        timestamp = 11.0 + index * 0.5
        rgb_bytes = bytes((index + value) % 256 for value in range(12))
        bgr_path = tmp_path / f"frame-{index}.bgr24"
        bgr_path.write_bytes(rgb_bytes)
        depth_bytes = struct.pack("<4f", *([10 + index] * 4))
        depth_path = tmp_path / f"frame-{index}.depth"
        depth_path.write_bytes(depth_bytes)
        ok, png = cv2.imencode(".png", np.frombuffer(rgb_bytes, dtype=np.uint8).reshape(2, 2, 3))
        assert ok
        png_path = tmp_path / f"frame-{index}.png"
        png_path.write_bytes(png.tobytes())
        frame_id = f"frame-{index}"
        row = copy.deepcopy(template)
        for label in ("task_label", "flight_label", "regression", "label_proof"):
            row.pop(label, None)
        row.update(
            sequence=index,
            frame_id=frame_id,
            observed_at_s=timestamp,
            next_frame_id=f"frame-{index + 1}" if index + 1 < count else None,
            proposed_task_label=None,
            proposed_flight_label=None,
            proposed_regression=None,
            truth_free=True,
            policy_applied_to_actuator=False,
        )
        context = row["observation"]["context"]
        context["aircraft_position"].update(altitude_m=0.0, altitude_reference="AGL")
        context.update(
            snapshot_id=frame_id,
            captured_at_s=timestamp,
            captured_at_utc=(origin + timedelta(seconds=index * 0.5)).isoformat(),
            airframe_profile_hash=episode["airframe_profile_sha256"],
            ground_speed_mps=float(index),
            airspeed_mps=float(index),
        )
        row["observation"]["event"].update(event_id=frame_id, observed_at_s=timestamp)
        telemetry = {
            "roll_deg": 0.0,
            "pitch_deg": 0.0,
            "in_allowed_zone": None,
            "position_healthy": None,
            "link_healthy": None,
            "flight_mode_allows_deploy": None,
            "release_zone_clear": None,
            "latitude_deg": 1.3,
            "longitude_deg": 103.8,
            "altitude_agl_m": 0.0,
            "gps_fix_type": 3,
            "gps_horizontal_accuracy_m": 1.0,
            "battery_remaining_pct": 80.0,
            "ground_speed_mps": float(index),
            "airspeed_mps": float(index),
            "wind_north_mps": 0.0,
            "wind_east_mps": 0.0,
            "geofence_healthy": True,
            "gps_observed_at_s": timestamp,
        }
        frame_telemetry = {
            **telemetry,
            "estimated_flight_time_s": 900.0,
            "map_cache_available": True,
        }
        frames.append(
            {
                "frame_id": frame_id,
                "sequence": index,
                "file": bgr_path.name,
                "bytes": len(rgb_bytes),
                "sha256": _hash(rgb_bytes),
                "provenance": {
                    "width": 2,
                    "height": 2,
                    "captured_at_s": timestamp,
                    "captured_at_clock": "host_monotonic",
                    "sensor_stamp_ns": 1_000_000_000 + index * 33_000_000,
                    "frame_telemetry": frame_telemetry,
                },
            }
        )
        depth_frames.append(
            {
                "frame_id": frame_id,
                "sequence": index,
                "file": depth_path.name,
                "bytes": len(depth_bytes),
                "sha256": _hash(depth_bytes),
            }
        )
        events.extend(
            [
                {
                    "schema_version": 1,
                    "kind": "telemetry",
                    "observed_at_s": timestamp,
                    "telemetry": telemetry,
                },
                {
                    "schema_version": 1,
                    "kind": "frame",
                    "frame_id": frame_id,
                    "captured_at_s": timestamp,
                    "image": {
                        "path": png_path.name,
                        "bytes": len(png),
                        "sha256": _hash(png.tobytes()),
                        "width": 2,
                        "height": 2,
                        "encoding": "png",
                    },
                },
            ]
        )
        row["source"].update(
            frame_id=frame_id,
            captured_at_s=timestamp,
            rgb_payload_sha256=_hash(rgb_bytes),
            depth_payload_sha256=_hash(depth_bytes),
        )
        _refresh_row(row)
        rows.append(row)
    event_path = tmp_path / "events.jsonl"
    event_path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    recording = RawSensorReplayRecording.from_jsonl(
        event_path,
        recording_id="fixture-recording",
        airframe_profile_hash="8" * 64,
        started_at_utc=origin,
        expected_sha256=_hash(event_path.read_bytes()),
        scenario_id=episode["scene_id"],
        scenario_manifest_hash="5" * 64,
        scenario_seed=episode["scenario_seed"],
    )
    raw_path = tmp_path / "recording.manifest.json"
    _write(
        raw_path,
        {
            "schema_version": 1,
            "recording_id": recording.recording_id,
            **recording.identity_mapping(),
            "recording_hash": recording.recording_hash,
        },
    )
    raw = json.loads(raw_path.read_text())
    raw.pop("frames")
    raw.pop("telemetry")
    raw["events"]["path"] = event_path.name
    _write(raw_path, raw)
    rgb = {
        "schema": "multidetect.gazebo-bgr24-frame-set",
        "schema_version": 1,
        "wire_version": 3,
        "scenario_id": episode["scene_id"],
        "scenario_manifest_hash": "5" * 64,
        "manifest_sha256": "1" * 64,
        "sensor_binding_hash": "2" * 64,
        "claims": {"actual_gazebo_pixels_persisted": True},
        "source_attribution": {
            "pixels": "actual_gazebo_camera_sensor_output",
            "wire": "MDGZSIM3",
            "per_frame_wire_telemetry_persisted": True,
            "wire_record_recomputed_from_persisted_bytes": True,
        },
        "per_frame_sensor_evidence": {"gps_and_pose_values_covered_by_wire_record_sha256": True},
        "repository_model_assets": {"assets_sha256": "3" * 64},
        "terrain_dem": {"file_sha256": "4" * 64},
        "frames": frames,
    }
    rgb_path = tmp_path / "rgb.manifest.json"
    depth_path = tmp_path / "depth.manifest.json"
    _write(rgb_path, rgb)
    _write(
        depth_path,
        {
            "schema": "multidetect.gazebo-depth-companion-set",
            "scenario_manifest_hash": "5" * 64,
            "frames": depth_frames,
            "source_attribution": {"truth_loaded_by_runtime": False},
        },
    )
    episode.update(
        recording_id=recording.recording_id,
        recording_hash=recording.recording_hash,
        transitions=rows,
    )
    group = _hash(
        "|".join(
            (
                recording.recording_hash,
                "5" * 64,
                "8" * 64,
                str(episode["seed"]),
                episode["task_contract_sha256"],
            )
        ).encode()
    )
    episode["episode_group_sha256"] = group
    for row in rows:
        row["source"].update(
            recording_id=recording.recording_id,
            recording_hash=recording.recording_hash,
            episode_group_sha256=group,
            rgb_manifest_sha256=_hash(rgb_path.read_bytes()),
            depth_manifest_sha256=_hash(depth_path.read_bytes()),
        )
    document["episodes"] = [episode]
    document["source_manifests"] = [
        {
            "path": str(rgb_path),
            "sha256": _hash(rgb_path.read_bytes()),
            "scenario_manifest_sha256": "5" * 64,
            "depth_manifest": {"path": str(depth_path), "sha256": _hash(depth_path.read_bytes())},
            "recording_manifest": {
                "path": str(raw_path),
                "sha256": _hash(raw_path.read_bytes()),
                "recording_id": recording.recording_id,
                "recording_hash": recording.recording_hash,
                "frame_count": count,
            },
        }
    ]
    path = tmp_path / "staging.json"
    _write(path, document)
    return path


def _run(path: Path) -> dict:
    return audit.audit_corpus([path], source_root=path.parent)


def test_native_reopen_canonical_features_and_three_clock_domains(tmp_path: Path) -> None:
    path = _fixture(tmp_path)
    before = path.read_bytes()
    report = _run(path)
    assert report["staging"][0]["source_verified"], report
    episode = report["staging"][0]["episodes"][0]
    assert episode["features"]["constant_count"] == 38
    assert episode["frame_identity_fields"] == ["recording_hash", "frame_id"]
    assert episode["time_domains"]["host_capture"]["duration_s"] == 3.5
    assert episode["time_domains"]["gazebo_sensor"]["duration_s"] == pytest.approx(0.231)
    assert not episode["time_domains"]["domains_joined_or_relabelled"]
    assert not report["observation_pretraining_eligible"]
    assert path.read_bytes() == before
    assert report["claim_boundary"]["action_labels_created"] is False


@pytest.mark.parametrize(
    "fault",
    ["future_event", "reversed", "features", "home", "profile", "truth", "position", "nonfinite"],
)
def test_invalid_observations_fail_closed(tmp_path: Path, fault: str) -> None:
    path = _fixture(tmp_path)
    document = json.loads(path.read_text())
    rows = document["episodes"][0]["transitions"]
    if fault == "future_event":
        rows[1]["observation"]["event"]["observed_at_s"] += 1
        _refresh_row(rows[1])
    elif fault == "reversed":
        rows.reverse()
    elif fault == "features":
        rows[1]["features"][0] += 0.1
    elif fault == "home":
        rows[1]["observation"]["context"]["home_position"]["latitude_deg"] += 0.001
        _refresh_row(rows[1])
    elif fault == "profile":
        rows[1]["observation"]["context"]["airframe_profile_hash"] = "7" * 64
        _refresh_row(rows[1])
    elif fault == "position":
        rows[1]["observation"]["context"]["aircraft_position"]["latitude_deg"] += 0.01
        _refresh_row(rows[1])
    elif fault == "nonfinite":
        rows[1]["features"][0] = float("inf")
    else:
        rows[1]["observation"]["event"]["target_world_position"] = [1, 2, 3]
    _write(path, document)
    report = _run(path)
    assert report["staging"][0]["errors"]
    assert not report["staging"][0]["source_verified"]
    assert not report["observation_pretraining_eligible"]


@pytest.mark.parametrize(
    "fault", ["missing", "hash_changed", "future_telemetry", "mixed_clock", "future_sensor_stamp"]
)
def test_source_failures_are_explicit(tmp_path: Path, fault: str) -> None:
    path = _fixture(tmp_path)
    document = json.loads(path.read_text())
    if fault == "missing":
        (tmp_path / "frame-0.png").unlink()
    elif fault == "hash_changed":
        (tmp_path / "frame-0.bgr24").write_bytes(b"changed")
    else:
        rgb_path = tmp_path / "rgb.manifest.json"
        rgb = json.loads(rgb_path.read_text())
        provenance = rgb["frames"][1]["provenance"]
        if fault == "future_telemetry":
            provenance["frame_telemetry"]["gps_observed_at_s"] += 1
        elif fault == "future_sensor_stamp":
            provenance["navsat_stamp_ns"] = provenance["sensor_stamp_ns"] + 1
        else:
            provenance["captured_at_clock"] = "host_perf_counter"
        _write(rgb_path, rgb)
        new_hash = _hash(rgb_path.read_bytes())
        document["source_manifests"][0]["sha256"] = new_hash
        for row in document["episodes"][0]["transitions"]:
            row["source"]["rgb_manifest_sha256"] = new_hash
        _write(path, document)
    report = _run(path)
    assert report["staging"][0]["errors"]
    assert not report["observation_pretraining_eligible"]


def test_defaulted_values_cannot_be_admitted_as_observed(tmp_path: Path) -> None:
    path = _fixture(tmp_path)
    document = json.loads(path.read_text())
    document["missing_context_fields"] = ["battery_remaining_pct"]
    _write(path, document)
    report = _run(path)
    assert report["staging"][0]["source_verified"]
    assert (
        "battery_remaining_pct" in report["staging"][0]["episodes"][0]["defaulted_context_fields"]
    )
    assert not report["observation_pretraining_eligible"]


def test_camera_relative_event_domain_is_preserved(tmp_path: Path) -> None:
    path = _fixture(tmp_path)
    document = json.loads(path.read_text())
    row = document["episodes"][0]["transitions"][1]
    row["observation"]["event"]["target_geometry_frame"] = "camera_relative"
    _refresh_row(row)
    _write(path, document)
    report = _run(path)
    assert report["staging"][0]["source_verified"], report["staging"][0]["errors"]
    assert audit._event(row["observation"]["event"]).target_geometry_frame == "camera_relative"


def test_cross_split_source_and_payload_contamination(tmp_path: Path) -> None:
    path = _fixture(tmp_path)
    second = tmp_path / "evaluation-staging.json"
    document = json.loads(path.read_text())
    document["episodes"][0]["split"] = "evaluation"
    _write(second, document)
    report = audit.audit_corpus([path, second], source_root=tmp_path)
    assert report["summary"]["source_verified_staging_count"] == 2
    assert report["summary"]["independent_recording_sources"] == 1
    assert report["summary"]["duplicate_recording_frame_observations"] == 8
    assert {entry["identity_kind"] for entry in report["split_contamination"]} == {
        "recording",
        "scenario_seed",
        "rgb_payload_sha256",
        "depth_payload_sha256",
    }
    assert not any(record["observation_pretraining_eligible"] for record in report["staging"])


def test_hash_is_rechecked_before_audit_seal(tmp_path: Path, monkeypatch) -> None:
    path = _fixture(tmp_path)
    original = audit._feature_summary

    def change_source(rows):
        (tmp_path / "frame-0.png").write_bytes(b"changed after verified read")
        return original(rows)

    monkeypatch.setattr(audit, "_feature_summary", change_source)
    report = _run(path)
    assert "source changed during audit" in report["staging"][0]["errors"][0]
    assert not report["observation_pretraining_eligible"]


def test_raw_recording_reversed_timeline_is_rejected(tmp_path: Path) -> None:
    path = _fixture(tmp_path)
    event_path = tmp_path / "events.jsonl"
    event_path.write_text("\n".join(reversed(event_path.read_text().splitlines())) + "\n")
    raw_path = tmp_path / "recording.manifest.json"
    raw = json.loads(raw_path.read_text())
    raw["events"].update(sha256=_hash(event_path.read_bytes()), bytes=event_path.stat().st_size)
    _write(raw_path, raw)
    document = json.loads(path.read_text())
    document["source_manifests"][0]["recording_manifest"]["sha256"] = _hash(raw_path.read_bytes())
    _write(path, document)
    report = _run(path)
    assert "ordered" in report["staging"][0]["errors"][0]
    assert not report["staging"][0]["source_verified"]


def test_cli_never_overwrites_evidence(tmp_path: Path) -> None:
    existing = tmp_path / "evidence.json"
    existing.write_bytes(b"original")
    with pytest.raises(SystemExit) as error:
        audit.main(["--staging", "not-needed.json", "--output", str(existing)])
    assert error.value.code == 2
    assert existing.read_bytes() == b"original"
