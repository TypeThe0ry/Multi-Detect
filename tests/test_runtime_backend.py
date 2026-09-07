from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

from multidetect.backends import (
    ObservationReplayBackend,
    ObservationRuntimeBackend,
    ReplayBackend,
)
from multidetect.clock import SystemClock
from multidetect.domain import FrameObservation, VehicleTelemetry
from multidetect.replay import ReplayRecording
from multidetect.runtime import (
    AutonomyRuntime,
    RuntimeBackend,
    RuntimeDescriptor,
    RuntimeMode,
    RuntimeRunReport,
    load_runtime_run_report,
    write_runtime_run_report,
)

AIRFRAME_HASH = "a" * 64
RUNTIME_REPORT_SCHEMA = (
    Path(__file__).resolve().parents[1] / "configs" / "schemas" / "runtime-run-report.schema.json"
)


def _frame(frame_id: str, captured_at_s: float) -> FrameObservation:
    return FrameObservation(
        frame_id=frame_id,
        captured_at_s=captured_at_s,
        detections=(),
        telemetry=VehicleTelemetry(
            altitude_agl_m=50.0,
            roll_deg=0.0,
            pitch_deg=0.0,
            ground_speed_mps=15.0,
            in_allowed_zone=True,
            geofence_healthy=True,
            position_healthy=True,
            link_healthy=True,
            flight_mode_allows_deploy=True,
            release_zone_clear=True,
        ),
    )


def test_explicit_observation_backend_names_preserve_legacy_api() -> None:
    assert ObservationRuntimeBackend is RuntimeBackend
    assert ObservationReplayBackend is ReplayBackend


def test_observation_replay_rejects_a_nondeterministic_clock() -> None:
    recording = ReplayRecording("clock-contract", (), AIRFRAME_HASH)

    with pytest.raises(TypeError, match="ManualClock"):
        ObservationReplayBackend(
            recording,
            clock=SystemClock(),  # type: ignore[arg-type]
        )


@dataclass(slots=True)
class _Processor:
    calls: list[tuple[str, float]] = field(default_factory=list)

    def process_observation(self, frame: FrameObservation, *, now_s: float) -> str:
        self.calls.append((frame.frame_id, now_s))
        return f"processed:{frame.frame_id}"


def test_replay_backend_uses_same_frame_observation_controller_path() -> None:
    frames = (_frame("f0", 1.0), _frame("f1", 1.25), _frame("f2", 2.0))
    backend = ReplayBackend(ReplayRecording("parity", frames, AIRFRAME_HASH))
    processor = _Processor()

    assert isinstance(backend, RuntimeBackend)
    assert not hasattr(backend, "frame_source")
    assert not hasattr(backend, "telemetry_provider")
    results = AutonomyRuntime(backend=backend, processor=processor).run()

    assert backend.descriptor is not None
    assert backend.descriptor.mode is RuntimeMode.REPLAY
    assert processor.calls == [("f0", 1.0), ("f1", 1.25), ("f2", 2.0)]
    assert tuple(item.result for item in results) == (
        "processed:f0",
        "processed:f1",
        "processed:f2",
    )


def test_runtime_max_frames_does_not_prefetch_or_advance_an_extra_frame() -> None:
    frames = (_frame("f0", 1.0), _frame("f1", 2.0))
    backend = ReplayBackend(ReplayRecording("bounded", frames, AIRFRAME_HASH))

    results = AutonomyRuntime(backend=backend, processor=_Processor()).run(max_frames=1)

    assert tuple(item.frame_id for item in results) == ("f0",)
    assert backend.clock.monotonic() == 1.0


def test_replay_backend_is_single_pass_and_fail_closed_after_close() -> None:
    backend = ReplayBackend(ReplayRecording("single", (_frame("f0", 0.0),), AIRFRAME_HASH))

    tuple(backend.observations())
    backend.close()
    with pytest.raises(RuntimeError, match="closed"):
        tuple(backend.observations())


def test_replay_backend_close_stops_an_active_observation_iterator() -> None:
    backend = ReplayBackend(
        ReplayRecording(
            "active-close",
            (_frame("f0", 0.0), _frame("f1", 1.0)),
            AIRFRAME_HASH,
        )
    )
    observations = backend.observations()

    assert next(observations).frame_id == "f0"
    backend.close()

    with pytest.raises(RuntimeError, match="closed"):
        next(observations)


def test_runtime_report_binds_descriptor_and_recording_hash() -> None:
    backend = ReplayBackend(ReplayRecording("report", [_frame("f0", 1.0)], AIRFRAME_HASH))

    results, report = AutonomyRuntime(backend=backend, processor=_Processor()).run_with_report()

    assert report.descriptor_hash == backend.descriptor.canonical_hash
    assert report.input_hash == backend.recording.recording_hash
    assert report.processed_frame_ids == ("f0",)
    assert tuple(item.frame_id for item in results) == report.processed_frame_ids
    assert report.stream_exhausted is True
    assert report.complete is True
    assert report.max_frames is None
    assert report.termination_reason == "stream_exhausted"


def test_runtime_report_round_trips_as_a_hash_bound_completion_record(tmp_path) -> None:
    backend = ReplayBackend(
        ReplayRecording("report-persist", [_frame("f0", 1.0), _frame("f1", 2.0)], AIRFRAME_HASH)
    )
    _results, report = AutonomyRuntime(backend=backend, processor=_Processor()).run_with_report()

    destination = write_runtime_run_report(tmp_path / "runtime-run.json", report)
    loaded = load_runtime_run_report(destination)

    assert loaded == report
    assert loaded.processed_frame_count == 2
    assert loaded.duration_s == report.finished_at_s - report.started_at_s
    assert loaded.run_identity_hash == loaded.canonical_hash
    assert loaded.to_mapping_with_hash()["sha256"] == loaded.canonical_hash


def test_runtime_report_matches_the_checked_in_json_schema() -> None:
    backend = ReplayBackend(ReplayRecording("report-schema", [_frame("f0", 1.0)], AIRFRAME_HASH))
    _results, report = AutonomyRuntime(backend=backend, processor=_Processor()).run_with_report()
    schema = json.loads(RUNTIME_REPORT_SCHEMA.read_text(encoding="utf-8"))

    Draft202012Validator(schema).validate(report.to_mapping_with_hash())

    invalid = report.to_mapping_with_hash()
    invalid["unexpected"] = True
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(invalid)


def test_runtime_report_marks_a_bounded_prefix_as_incomplete() -> None:
    backend = ReplayBackend(
        ReplayRecording("report-prefix", [_frame("f0", 1.0), _frame("f1", 2.0)], AIRFRAME_HASH)
    )

    _results, report = AutonomyRuntime(backend=backend, processor=_Processor()).run_with_report(
        max_frames=1
    )

    assert report.processed_frame_ids == ("f0",)
    assert report.stream_exhausted is False
    assert report.complete is False
    assert report.max_frames == 1
    assert report.termination_reason == "frame_limit_reached"


def test_runtime_report_rejects_derived_field_tamper(tmp_path) -> None:
    backend = ReplayBackend(ReplayRecording("report-tamper", [_frame("f0", 1.0)], AIRFRAME_HASH))
    _results, report = AutonomyRuntime(backend=backend, processor=_Processor()).run_with_report()
    document = report.to_mapping_with_hash()

    document["processed_frame_count"] = 99
    with pytest.raises(ValueError, match="processed_frame_count"):
        RuntimeRunReport.from_mapping(document)

    document = report.to_mapping_with_hash()
    document["duration_s"] = 99.0
    with pytest.raises(ValueError, match="duration_s"):
        RuntimeRunReport.from_mapping(document)

    document = report.to_mapping_with_hash()
    document["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="sha256"):
        RuntimeRunReport.from_mapping(document)

    destination = write_runtime_run_report(tmp_path / "runtime-run.json", report)
    destination.write_text(
        destination.read_text(encoding="utf-8").replace(
            '"stream_exhausted":true', '"stream_exhausted":false'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="sha256|termination_reason"):
        load_runtime_run_report(destination)


def test_runtime_rejects_backend_provenance_swap_during_processing() -> None:
    backend = ReplayBackend(
        ReplayRecording("mutable-provenance", [_frame("f0", 1.0)], AIRFRAME_HASH)
    )

    class _MutatingProcessor:
        def process_observation(self, frame: FrameObservation, *, now_s: float) -> str:
            del frame, now_s
            backend.descriptor = RuntimeDescriptor(
                mode=RuntimeMode.REPLAY,
                profile=backend.descriptor.profile,
                backend_id="replay:swapped",
                airframe_profile_hash=AIRFRAME_HASH,
                recording_hash=backend.recording.recording_hash,
                deterministic=True,
            )
            return "mutated"

    with pytest.raises(RuntimeError, match="provenance changed"):
        AutonomyRuntime(backend=backend, processor=_MutatingProcessor()).run_with_report()

    assert backend._closed is True


def test_runtime_rejects_backend_input_hash_swap_during_processing() -> None:
    backend = ReplayBackend(ReplayRecording("mutable-input", [_frame("f0", 1.0)], AIRFRAME_HASH))

    class _MutatingProcessor:
        def process_observation(self, frame: FrameObservation, *, now_s: float) -> str:
            del frame, now_s
            backend.recording = ReplayRecording(
                "swapped-input",
                [_frame("f1", 1.0)],
                AIRFRAME_HASH,
            )
            return "mutated"

    with pytest.raises(RuntimeError, match="provenance changed"):
        AutonomyRuntime(backend=backend, processor=_MutatingProcessor()).run_with_report()

    assert backend._closed is True


def test_runtime_closes_backend_when_observation_iterator_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = ReplayBackend(
        ReplayRecording("iterator-failure", (_frame("f0", 1.0),), AIRFRAME_HASH)
    )

    def _raise_on_observations(_backend: ReplayBackend) -> object:
        raise RuntimeError("observation stream unavailable")

    monkeypatch.setattr(ReplayBackend, "observations", _raise_on_observations)

    with pytest.raises(RuntimeError, match="observation stream unavailable"):
        AutonomyRuntime(backend=backend, processor=_Processor()).run_with_report()

    assert backend._closed is True


@pytest.mark.parametrize("invalid", [True, 1.5, "1"])
def test_runtime_rejects_non_integer_max_frames(invalid: object) -> None:
    backend = ReplayBackend(ReplayRecording("invalid-max", [_frame("f0", 1.0)], AIRFRAME_HASH))
    with pytest.raises(TypeError, match="integer"):
        AutonomyRuntime(backend=backend, processor=_Processor()).run(
            max_frames=invalid  # type: ignore[arg-type]
        )


def test_runtime_report_rejects_bad_hash_time_and_duplicate_frames() -> None:
    with pytest.raises(ValueError, match="64 hexadecimal"):
        RuntimeRunReport("bad", "b" * 64, (), 0.0, 0.0)
    with pytest.raises(ValueError, match="finite"):
        RuntimeRunReport("a" * 64, "b" * 64, (), 0.0, float("nan"))
    with pytest.raises(ValueError, match="unique"):
        RuntimeRunReport("a" * 64, "b" * 64, ["f0", "f0"], 0.0, 1.0)
