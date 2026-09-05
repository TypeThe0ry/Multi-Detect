from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from multidetect.autonomy import AutonomyContractError
from multidetect.backends import (
    RawSensorReplayBackend,
    RawSensorReplayRecording,
    RawSensorRuntimeBackend,
)
from multidetect.clock import ManualClock, SystemClock
from multidetect.runtime import RuntimeDescriptor, RuntimeMode, RuntimeProfile
from multidetect.simulation import load_runtime_scenario

ROOT = Path(__file__).resolve().parents[1]
SCENARIO_PATH = ROOT / "configs/scenarios/river_rescue_vtol_001.scenario.json"
AIRFRAME_HASH = "a" * 64
STARTED_AT_UTC = "2026-08-25T00:00:00Z"


@dataclass(frozen=True)
class _Pixels:
    height: int
    width: int
    payload: bytes

    @property
    def shape(self) -> tuple[int, int, int]:
        return (self.height, self.width, 3)


def _decode(payload: bytes, reference) -> _Pixels:
    return _Pixels(reference.height, reference.width, payload)


def _telemetry(altitude_m: float) -> dict[str, object]:
    return {
        "altitude_agl_m": altitude_m,
        "roll_deg": 1.0,
        "pitch_deg": -2.0,
        "ground_speed_mps": 15.0,
        "in_allowed_zone": True,
        "geofence_healthy": True,
        "position_healthy": True,
        "link_healthy": True,
        "flight_mode_allows_deploy": False,
        "release_zone_clear": None,
        "latitude_deg": 1.3521,
        "longitude_deg": 103.8198,
        "gps_fix_type": 3,
        "satellites_visible": 15,
        "altitude_observed_at_s": 1.0,
        "attitude_observed_at_s": 1.0,
        "position_observed_at_s": 1.0,
    }


def _image_event(
    *,
    frame_id: str,
    captured_at_s: float,
    image_name: str,
    payload: bytes,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "frame",
        "frame_id": frame_id,
        "captured_at_s": captured_at_s,
        "image": {
            "path": image_name,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
            "width": 3,
            "height": 2,
            "encoding": "png",
        },
    }


def _telemetry_event(*, observed_at_s: float, altitude_m: float) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "telemetry",
        "observed_at_s": observed_at_s,
        "telemetry": _telemetry(altitude_m),
    }


def test_legacy_raw_telemetry_omits_absent_mission_feedback_fields(tmp_path: Path) -> None:
    recording, _event_path, _images = _load_recording(tmp_path)

    telemetry = recording.telemetry_samples[0].to_mapping()["telemetry"]

    assert "mission_sequence_observed_at_s" not in telemetry
    assert "mission_item_reached_sequence" not in telemetry
    assert "mission_item_reached_observed_at_s" not in telemetry
    assert "gps_absolute_altitude_m" not in telemetry


def test_raw_telemetry_round_trips_absolute_gps_altitude(tmp_path: Path) -> None:
    first = _telemetry_event(observed_at_s=1.0, altitude_m=10.0)
    first_telemetry = first["telemetry"]
    assert isinstance(first_telemetry, dict)
    first_telemetry["gps_absolute_altitude_m"] = 47.75
    events = [
        first,
        _image_event(
            frame_id="frame-0001",
            captured_at_s=1.0,
            image_name="frame-0001.png",
            payload=b"encoded-frame-one",
        ),
    ]

    recording, _event_path, _images = _load_recording(
        tmp_path,
        with_scenario=False,
        events=events,
    )

    sample = recording.telemetry_samples[0]
    assert sample.telemetry.gps_absolute_altitude_m == pytest.approx(47.75)
    assert sample.to_mapping()["telemetry"]["gps_absolute_altitude_m"] == pytest.approx(47.75)


def test_raw_telemetry_retains_observed_mission_feedback_fields(tmp_path: Path) -> None:
    first = _telemetry_event(observed_at_s=1.0, altitude_m=10.0)
    first_telemetry = first["telemetry"]
    assert isinstance(first_telemetry, dict)
    first_telemetry.update(
        {
            "mission_sequence": 2,
            "mission_sequence_observed_at_s": 1.0,
            "mission_item_reached_sequence": 1,
            "mission_item_reached_observed_at_s": 0.9,
        }
    )
    events = [
        first,
        _image_event(
            frame_id="frame-0001",
            captured_at_s=1.0,
            image_name="frame-0001.png",
            payload=b"encoded-frame-one",
        ),
        _telemetry_event(observed_at_s=1.5, altitude_m=20.0),
        _image_event(
            frame_id="frame-0002",
            captured_at_s=2.0,
            image_name="frame-0002.png",
            payload=b"encoded-frame-two",
        ),
    ]
    recording, _event_path, _images = _load_recording(tmp_path, events=events)

    telemetry = recording.telemetry_samples[0].to_mapping()["telemetry"]

    assert telemetry["mission_sequence"] == 2
    assert telemetry["mission_sequence_observed_at_s"] == pytest.approx(1.0)
    assert telemetry["mission_item_reached_sequence"] == 1
    assert telemetry["mission_item_reached_observed_at_s"] == pytest.approx(0.9)


def _write_events(
    tmp_path: Path,
    *,
    events: list[dict[str, object]] | None = None,
) -> tuple[Path, str, int, tuple[Path, Path]]:
    first_payload = b"encoded-frame-one"
    second_payload = b"encoded-frame-two"
    first_image = tmp_path / "frame-0001.png"
    second_image = tmp_path / "frame-0002.png"
    first_image.write_bytes(first_payload)
    second_image.write_bytes(second_payload)
    selected_events = events or [
        _telemetry_event(observed_at_s=1.0, altitude_m=10.0),
        _image_event(
            frame_id="frame-0001",
            captured_at_s=1.0,
            image_name=first_image.name,
            payload=first_payload,
        ),
        _telemetry_event(observed_at_s=1.5, altitude_m=20.0),
        _image_event(
            frame_id="frame-0002",
            captured_at_s=2.0,
            image_name=second_image.name,
            payload=second_payload,
        ),
    ]
    payload = "".join(
        json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n" for item in selected_events
    ).encode("utf-8")
    events_path = tmp_path / "events.jsonl"
    events_path.write_bytes(payload)
    return (
        events_path,
        hashlib.sha256(payload).hexdigest(),
        len(payload),
        (first_image, second_image),
    )


def _load_recording(
    tmp_path: Path,
    *,
    with_scenario: bool = True,
    events: list[dict[str, object]] | None = None,
) -> tuple[RawSensorReplayRecording, Path, tuple[Path, Path]]:
    event_path, event_hash, event_bytes, images = _write_events(tmp_path, events=events)
    scenario = (
        load_runtime_scenario(SCENARIO_PATH, mode=RuntimeMode.REPLAY) if with_scenario else None
    )
    recording = RawSensorReplayRecording.from_jsonl(
        event_path,
        recording_id="raw-fixture",
        airframe_profile_hash=AIRFRAME_HASH,
        started_at_utc=STARTED_AT_UTC,
        expected_sha256=event_hash,
        expected_byte_count=event_bytes,
        scenario_id=scenario.scenario_id if scenario is not None else None,
        scenario_manifest_hash=(scenario.scenario_manifest_hash if scenario is not None else None),
        scenario_seed=scenario.seed if scenario is not None else None,
        scenario_input=scenario,
    )
    return recording, event_path, images


def _write_manifest(
    tmp_path: Path,
    recording: RawSensorReplayRecording,
    event_path: Path,
    *,
    mutate: dict[str, object] | None = None,
) -> tuple[Path, str]:
    manifest: dict[str, object] = {
        "schema_version": 1,
        "recording_id": recording.recording_id,
        "airframe_profile_hash": recording.airframe_profile_hash,
        "started_at_utc": recording.started_at_utc.isoformat().replace("+00:00", "Z"),
        "recording_hash": recording.recording_hash,
        "events": {
            "path": event_path.name,
            "sha256": recording.event_stream_sha256,
            "bytes": recording.event_stream_bytes,
        },
    }
    if recording.scenario_id is not None:
        manifest["scenario"] = {
            "scenario_id": recording.scenario_id,
            "scenario_manifest_hash": recording.scenario_manifest_hash,
            "scenario_seed": recording.scenario_seed,
        }
    if mutate:
        manifest.update(mutate)
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    path = tmp_path / "recording.manifest.json"
    path.write_bytes(payload)
    return path, hashlib.sha256(payload).hexdigest()


def test_manifest_replay_drives_raw_frames_time_aligned_telemetry_and_descriptor(
    tmp_path: Path,
) -> None:
    recording, event_path, _ = _load_recording(tmp_path)
    manifest_path, manifest_hash = _write_manifest(tmp_path, recording, event_path)
    scenario = load_runtime_scenario(SCENARIO_PATH, mode=RuntimeMode.REPLAY)

    backend = RawSensorReplayBackend.from_manifest(
        manifest_path,
        expected_manifest_sha256=manifest_hash,
        scenario_input=scenario,
        image_decoder=_decode,
    )

    assert isinstance(backend, RawSensorRuntimeBackend)
    assert backend.descriptor.mode is RuntimeMode.REPLAY
    assert backend.descriptor.deterministic is True
    assert backend.descriptor.recording_hash == recording.recording_hash
    assert backend.descriptor.airframe_profile_hash == AIRFRAME_HASH
    assert backend.descriptor.scenario_id == scenario.scenario_id
    assert backend.descriptor.scenario_manifest_hash == scenario.scenario_manifest_hash
    assert backend.descriptor.scenario_seed == scenario.seed
    assert backend.input_hash == recording.recording_hash
    assert backend.frame_count == 2
    assert backend.frame_source.frame_count == 2
    assert backend.clock.utc_now() == datetime(2026, 8, 25, tzinfo=timezone.utc)
    assert not hasattr(backend, "detections")
    assert not hasattr(backend, "truth")
    assert not hasattr(backend, "expectations")

    backend.frame_source.open()
    first = backend.frame_source.read()
    assert first.frame_id == "frame-0001"
    assert first.image_bgr.payload == b"encoded-frame-one"
    assert backend.clock.monotonic() == 1.0
    assert backend.clock.utc_now() == datetime(2026, 8, 25, 0, 0, 1, tzinfo=timezone.utc)
    assert (
        backend.telemetry_provider.snapshot(now_s=backend.clock.monotonic()).altitude_agl_m == 10.0
    )

    second = backend.frame_source.read()
    assert second.frame_id == "frame-0002"
    assert backend.clock.monotonic() == 2.0
    assert (
        backend.telemetry_provider.snapshot(now_s=backend.clock.monotonic()).altitude_agl_m == 20.0
    )


def test_raw_replay_normalizes_mavlink_unknown_satellite_sentinel(
    tmp_path: Path,
) -> None:
    """Wire value 255 means unknown, never 255 usable satellites."""

    events = [
        _telemetry_event(observed_at_s=1.0, altitude_m=10.0),
        _image_event(
            frame_id="frame-0001",
            captured_at_s=1.0,
            image_name="frame-0001.png",
            payload=b"encoded-frame-one",
        ),
    ]
    events[0]["telemetry"]["satellites_visible"] = 255  # type: ignore[index]
    recording, _, _ = _load_recording(tmp_path, with_scenario=False, events=events)

    assert recording.telemetry_samples[0].telemetry.satellites_visible is None


def test_raw_replay_keeps_valid_satellite_count(
    tmp_path: Path,
) -> None:
    events = [
        _telemetry_event(observed_at_s=1.0, altitude_m=10.0),
        _image_event(
            frame_id="frame-0001",
            captured_at_s=1.0,
            image_name="frame-0001.png",
            payload=b"encoded-frame-one",
        ),
    ]
    events[0]["telemetry"]["satellites_visible"] = 254  # type: ignore[index]
    recording, _, _ = _load_recording(tmp_path, with_scenario=False, events=events)

    assert recording.telemetry_samples[0].telemetry.satellites_visible == 254


def test_raw_replay_runs_the_caller_supplied_detector_not_recorded_detections(
    tmp_path: Path,
) -> None:
    recording, _, _ = _load_recording(tmp_path, with_scenario=False)
    backend = RawSensorReplayBackend(recording, image_decoder=_decode)
    observed_payloads: list[bytes] = []

    def production_detector(image: _Pixels) -> tuple[object, ...]:
        observed_payloads.append(image.payload)
        return ()

    backend.frame_source.open()
    while len(observed_payloads) < len(recording.frames):
        captured = backend.frame_source.read()
        production_detector(captured.image_bgr)
        backend.telemetry_provider.snapshot(now_s=backend.clock.monotonic())

    assert observed_payloads == [b"encoded-frame-one", b"encoded-frame-two"]


def test_frame_source_is_single_pass_and_active_iterator_fails_after_close(
    tmp_path: Path,
) -> None:
    recording, _, _ = _load_recording(tmp_path, with_scenario=False)
    backend = RawSensorReplayBackend(recording, image_decoder=_decode)
    iterator = backend.frame_source.frames()

    assert next(iterator).frame_id == "frame-0001"
    backend.frame_source.close()

    with pytest.raises(RuntimeError, match="closed"):
        next(iterator)
    with pytest.raises(RuntimeError, match="closed"):
        backend.frame_source.read()


def test_completed_frame_source_cannot_create_a_second_iterator(tmp_path: Path) -> None:
    recording, _, _ = _load_recording(tmp_path, with_scenario=False)
    backend = RawSensorReplayBackend(recording, image_decoder=_decode)

    assert [frame.frame_id for frame in backend.frame_source.frames()] == [
        "frame-0001",
        "frame-0002",
    ]
    with pytest.raises(RuntimeError, match="single-pass"):
        tuple(backend.frame_source.frames())


def test_telemetry_provider_and_backend_close_fail_closed_and_are_idempotent(
    tmp_path: Path,
) -> None:
    recording, _, _ = _load_recording(tmp_path, with_scenario=False)
    backend = RawSensorReplayBackend(recording, image_decoder=_decode)
    backend.frame_source.open()
    backend.frame_source.read()
    backend.telemetry_provider.close()

    with pytest.raises(RuntimeError, match="closed"):
        backend.telemetry_provider.snapshot(now_s=1.0)

    backend.close()
    backend.close()
    with pytest.raises(RuntimeError, match="closed"):
        backend.frame_source.open()


def test_image_is_rehashed_at_read_and_clock_does_not_advance_on_mutation(
    tmp_path: Path,
) -> None:
    recording, _, images = _load_recording(tmp_path, with_scenario=False)
    backend = RawSensorReplayBackend(recording, image_decoder=_decode)
    images[0].write_bytes(b"Encoded-frame-one")
    backend.frame_source.open()

    with pytest.raises(AutonomyContractError, match="hash mismatch"):
        backend.frame_source.read()

    assert backend.clock.monotonic() == 0.0


@pytest.mark.parametrize("forbidden", ["detections", "truth", "expectations"])
def test_event_schema_rejects_answer_or_precomputed_detection_fields(
    tmp_path: Path,
    forbidden: str,
) -> None:
    event_path, _, _, _ = _write_events(tmp_path)
    lines = event_path.read_text(encoding="utf-8").splitlines()
    raw = json.loads(lines[1])
    raw[forbidden] = []
    lines[1] = json.dumps(raw)
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    event_path.write_bytes(payload)

    with pytest.raises(AutonomyContractError, match="unknown field"):
        RawSensorReplayRecording.from_jsonl(
            event_path,
            recording_id="forbidden",
            airframe_profile_hash=AIRFRAME_HASH,
            started_at_utc=STARTED_AT_UTC,
            expected_sha256=hashlib.sha256(payload).hexdigest(),
        )


def test_jsonl_requires_an_external_expected_hash_and_rejects_mismatch(
    tmp_path: Path,
) -> None:
    event_path, event_hash, _, _ = _write_events(tmp_path)

    with pytest.raises(AutonomyContractError, match="requires expected_sha256"):
        RawSensorReplayRecording.from_jsonl(
            event_path,
            recording_id="missing-hash",
            airframe_profile_hash=AIRFRAME_HASH,
            started_at_utc=STARTED_AT_UTC,
        )
    with pytest.raises(AutonomyContractError, match="hash mismatch"):
        RawSensorReplayRecording.from_jsonl(
            event_path,
            recording_id="bad-hash",
            airframe_profile_hash=AIRFRAME_HASH,
            started_at_utc=STARTED_AT_UTC,
            expected_sha256="f" * 64,
        )

    # Compatibility alias is equally strict.
    assert (
        RawSensorReplayRecording.from_jsonl(
            event_path,
            recording_id="alias",
            airframe_profile_hash=AIRFRAME_HASH,
            started_at_utc=STARTED_AT_UTC,
            jsonl_sha256=event_hash,
        ).event_stream_sha256
        == event_hash
    )


def test_manifest_rejects_event_path_escape_before_reading_outside_file(
    tmp_path: Path,
) -> None:
    recording, event_path, _ = _load_recording(tmp_path)
    manifest_path, _ = _write_manifest(
        tmp_path,
        recording,
        event_path,
        mutate={
            "events": {
                "path": "../outside.jsonl",
                "sha256": recording.event_stream_sha256,
                "bytes": recording.event_stream_bytes,
            }
        },
    )

    with pytest.raises(AutonomyContractError, match="relative and contained"):
        RawSensorReplayBackend.from_manifest(manifest_path, image_decoder=_decode)


def test_manifest_rejects_wrong_schema_recording_hash_and_external_manifest_hash(
    tmp_path: Path,
) -> None:
    recording, event_path, _ = _load_recording(tmp_path)
    bad_schema, _ = _write_manifest(tmp_path, recording, event_path, mutate={"schema_version": 2})
    with pytest.raises(AutonomyContractError, match="unsupported schema_version"):
        RawSensorReplayBackend.from_manifest(bad_schema, image_decoder=_decode)

    bad_recording, _ = _write_manifest(
        tmp_path, recording, event_path, mutate={"recording_hash": "f" * 64}
    )
    with pytest.raises(AutonomyContractError, match="recording_hash"):
        RawSensorReplayBackend.from_manifest(bad_recording, image_decoder=_decode)

    valid_manifest, _ = _write_manifest(tmp_path, recording, event_path)
    with pytest.raises(AutonomyContractError, match="manifest hash mismatch"):
        RawSensorReplayBackend.from_manifest(
            valid_manifest,
            expected_manifest_sha256="f" * 64,
            image_decoder=_decode,
        )


def test_manifest_requires_a_strict_utc_anchor_and_anchor_changes_recording_identity(
    tmp_path: Path,
) -> None:
    recording, event_path, _ = _load_recording(tmp_path)
    manifest_path, _ = _write_manifest(tmp_path, recording, event_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("started_at_utc")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(AutonomyContractError, match="started_at_utc"):
        RawSensorReplayBackend.from_manifest(manifest_path, image_decoder=_decode)

    manifest["started_at_utc"] = "2026-08-25T08:00:00+08:00"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(AutonomyContractError, match=r"Z or \+00:00"):
        RawSensorReplayBackend.from_manifest(manifest_path, image_decoder=_decode)

    shifted = RawSensorReplayRecording.from_jsonl(
        event_path,
        recording_id=recording.recording_id,
        airframe_profile_hash=AIRFRAME_HASH,
        started_at_utc="2026-08-25T00:00:01Z",
        expected_sha256=recording.event_stream_sha256,
        expected_byte_count=recording.event_stream_bytes,
        scenario_id=recording.scenario_id,
        scenario_manifest_hash=recording.scenario_manifest_hash,
        scenario_seed=recording.scenario_seed,
    )
    assert shifted.recording_hash != recording.recording_hash


def test_scenario_input_and_descriptor_must_match_recording_identity(
    tmp_path: Path,
) -> None:
    recording, event_path, _ = _load_recording(tmp_path)
    manifest_path, _ = _write_manifest(tmp_path, recording, event_path)
    scenario = load_runtime_scenario(SCENARIO_PATH, mode=RuntimeMode.REPLAY)
    mismatched = type(scenario)(
        scenario_id=scenario.scenario_id,
        scenario_version=scenario.scenario_version,
        scenario_manifest_hash=scenario.scenario_manifest_hash,
        seed=scenario.seed + 1,
        world=scenario.world,
        vehicle=scenario.vehicle,
        sensors=scenario.sensors,
        faults=scenario.faults,
    )

    with pytest.raises(AutonomyContractError, match="does not match"):
        RawSensorReplayBackend.from_manifest(
            manifest_path,
            scenario_input=mismatched,
            image_decoder=_decode,
        )

    bad_descriptor = RuntimeDescriptor(
        mode=RuntimeMode.REPLAY,
        profile=RuntimeProfile.replay(),
        backend_id="raw-replay:wrong-id",
        airframe_profile_hash=recording.airframe_profile_hash,
        scenario_id=recording.scenario_id,
        scenario_manifest_hash=recording.scenario_manifest_hash,
        scenario_seed=recording.scenario_seed,
        recording_hash=recording.recording_hash,
        deterministic=True,
    )
    with pytest.raises(ValueError, match="backend_id"):
        RawSensorReplayBackend(
            recording,
            descriptor=bad_descriptor,
            image_decoder=_decode,
        )


def test_raw_replay_rejects_nondeterministic_or_forward_started_clock(
    tmp_path: Path,
) -> None:
    recording, _, _ = _load_recording(tmp_path, with_scenario=False)

    with pytest.raises(TypeError, match="ManualClock"):
        RawSensorReplayBackend(
            recording,
            clock=SystemClock(),  # type: ignore[arg-type]
            image_decoder=_decode,
        )
    with pytest.raises(ValueError, match="starts after"):
        RawSensorReplayBackend(
            recording,
            clock=ManualClock(
                initial_monotonic_s=1.1,
                initial_utc=recording.started_at_utc + timedelta(seconds=1.1),
            ),
            image_decoder=_decode,
        )

    with pytest.raises(ValueError, match="UTC.*anchor"):
        RawSensorReplayBackend(
            recording,
            clock=ManualClock(),
            image_decoder=_decode,
        )


def test_telemetry_snapshot_rejects_clock_skew_and_stale_samples(tmp_path: Path) -> None:
    recording, _, _ = _load_recording(tmp_path, with_scenario=False)
    backend = RawSensorReplayBackend(
        recording,
        image_decoder=_decode,
        maximum_telemetry_age_s=0.25,
        maximum_clock_skew_s=1e-6,
    )
    backend.frame_source.open()
    backend.frame_source.read()

    with pytest.raises(RuntimeError, match="not synchronized"):
        backend.telemetry_provider.snapshot(now_s=backend.clock.monotonic() + 0.01)

    assert (
        backend.telemetry_provider.snapshot(now_s=backend.clock.monotonic()).altitude_agl_m == 10.0
    )
    backend.frame_source.read()
    with pytest.raises(RuntimeError, match="stale"):
        backend.telemetry_provider.snapshot(now_s=backend.clock.monotonic())


def test_timeline_rejects_out_of_order_events_and_late_first_telemetry(
    tmp_path: Path,
) -> None:
    first_payload = b"encoded-frame-one"
    out_of_order = [
        _telemetry_event(observed_at_s=2.0, altitude_m=20.0),
        _image_event(
            frame_id="frame-0001",
            captured_at_s=1.0,
            image_name="frame-0001.png",
            payload=first_payload,
        ),
    ]
    event_path, event_hash, _, _ = _write_events(tmp_path, events=out_of_order)
    with pytest.raises(AutonomyContractError, match="ordered"):
        RawSensorReplayRecording.from_jsonl(
            event_path,
            recording_id="unordered",
            airframe_profile_hash=AIRFRAME_HASH,
            started_at_utc=STARTED_AT_UTC,
            expected_sha256=event_hash,
        )

    late_telemetry = [
        _image_event(
            frame_id="frame-0001",
            captured_at_s=1.0,
            image_name="frame-0001.png",
            payload=first_payload,
        ),
        _telemetry_event(observed_at_s=2.0, altitude_m=20.0),
    ]
    event_path, event_hash, _, _ = _write_events(tmp_path, events=late_telemetry)
    with pytest.raises(AutonomyContractError, match="at or before the first frame"):
        RawSensorReplayRecording.from_jsonl(
            event_path,
            recording_id="late-telemetry",
            airframe_profile_hash=AIRFRAME_HASH,
            started_at_utc=STARTED_AT_UTC,
            expected_sha256=event_hash,
        )


def test_custom_decoder_must_return_manifest_dimensions(tmp_path: Path) -> None:
    recording, _, _ = _load_recording(tmp_path, with_scenario=False)
    backend = RawSensorReplayBackend(
        recording,
        image_decoder=lambda _payload, _reference: _Pixels(1, 1, b"wrong"),
    )
    backend.frame_source.open()

    with pytest.raises(AutonomyContractError, match="dimensions"):
        backend.frame_source.read()
    assert backend.clock.monotonic() == 0.0
