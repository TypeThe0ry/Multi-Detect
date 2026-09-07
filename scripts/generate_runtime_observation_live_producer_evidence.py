"""Generate software evidence for the profile-bound live observation producer.

The harness deliberately uses the real ``LiveMissionRunner`` and
``RuntimeObservationResult`` path with deterministic in-memory raw sensors.  It
does not open a camera or Pixhawk link; the generated files therefore prove
producer wiring, frame identity, termination and counter separation only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

if __package__ in {None, ""}:
    _repository_source = Path(__file__).resolve().parents[1] / "src"
    if _repository_source.is_dir() and str(_repository_source) not in sys.path:
        sys.path.insert(0, str(_repository_source))

from multidetect.clock import ManualClock
from multidetect.config import MissionConfig
from multidetect.domain import BoundingBox, Detection, SensorKind, VehicleTelemetry
from multidetect.live import LiveMissionRunner, LiveRunConfig
from multidetect.mission import MissionController
from multidetect.product_version import PRODUCT_VERSION
from multidetect.runtime import RuntimeDescriptor, RuntimeMode, RuntimeProfile
from multidetect.runtime_observation import (
    load_runtime_observation_result,
    write_runtime_observation_result,
)
from multidetect.runtime_report_binding import verify_runtime_report_binding
from multidetect.vision import CapturedFrame

ITERATION = "v189"


class _FrameSource:
    def __init__(self, clock: ManualClock, frame_count: int) -> None:
        self.clock = clock
        self.frame_count = frame_count
        self.next_index = 0

    def open(self) -> None:
        return None

    def read(self) -> CapturedFrame:
        if self.next_index >= self.frame_count:
            raise StopIteration
        self.next_index += 1
        captured_at_s = 100.0 + (self.next_index - 1) * 0.1
        self.clock.advance_to(captured_at_s)
        return CapturedFrame(
            frame_id=f"live-producer-{self.next_index:04d}",
            captured_at_s=captured_at_s,
            image_bgr=object(),
            width=640,
            height=480,
        )

    def close(self) -> None:
        return None


class _Detector:
    def detect(self, _image: object) -> tuple[Detection, ...]:
        return (
            Detection(
                "flame",
                0.95,
                BoundingBox(0.2, 0.2, 0.4, 0.5),
                SensorKind.RGB,
                "v189-live-producer",
            ),
        )

    def covers_labels(self, _labels: object) -> bool:
        return False


class _Telemetry:
    def __init__(self, *, application_messages: int = 0) -> None:
        self.telemetry_request_messages_transmitted = 2
        self.messages_transmitted = (
            self.telemetry_request_messages_transmitted + application_messages
        )
        self.application_messages_transmitted = application_messages
        self.flight_control_messages_transmitted = application_messages
        self.is_read_only = application_messages == 0
        self.hardware_control_enabled = application_messages != 0
        self.flight_control_enabled = application_messages != 0
        self.output_locked = application_messages == 0

    def snapshot(self, *, now_s: float) -> VehicleTelemetry:
        del now_s
        return VehicleTelemetry(
            altitude_agl_m=20.0,
            roll_deg=0.0,
            pitch_deg=0.0,
            ground_speed_mps=1.0,
            in_allowed_zone=True,
            geofence_healthy=True,
            position_healthy=True,
            link_healthy=True,
            flight_mode_allows_deploy=True,
            release_zone_clear=True,
            person_detector_healthy=True,
        )

    def close(self) -> None:
        return None


class _Backend:
    def __init__(self, *, clock: ManualClock, telemetry: _Telemetry) -> None:
        self.clock = clock
        self.frame_source = _FrameSource(clock, frame_count=2)
        self.telemetry_provider = telemetry
        self.descriptor = RuntimeDescriptor(
            mode=RuntimeMode.HIL,
            profile=RuntimeProfile.hil(
                vehicle_identity_hash="1" * 64,
                airframe_profile_hash="2" * 64,
                endpoint_binding_hash="3" * 64,
            ),
            backend_id="evidence:live-hil-producer",
            airframe_profile_hash="2" * 64,
            scenario_id="v189-live-producer",
            scenario_manifest_hash="4" * 64,
            scenario_seed=189,
            deterministic=False,
        )


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def _summary(path: Path) -> dict[str, object]:
    payload = path.read_bytes()
    return {
        "path": path.name,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _schema_errors(path: Path, schema_path: Path) -> int:
    document = json.loads(path.read_text(encoding="utf-8"))
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return sum(1 for _ in Draft202012Validator(schema).iter_errors(document))


def build_evidence(output_directory: Path, schema_path: Path) -> dict[str, object]:
    output_directory = output_directory.expanduser().resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    clock = ManualClock(initial_monotonic_s=100.0)
    telemetry = _Telemetry()
    backend = _Backend(clock=clock, telemetry=telemetry)
    runner = LiveMissionRunner.from_runtime_backend(
        backend=backend,
        mission=MissionController(
            MissionConfig.from_json(
                Path(__file__).resolve().parents[1] / "configs/missions/fire_patrol.demo.json"
            )
        ),
        detector=_Detector(),
        config=LiveRunConfig(max_frames=2, display=False),
    )
    run_result = runner.run()
    report = runner.runtime_run_report
    observation = runner.build_runtime_observation_result()
    runtime_message_counters = runner.runtime_message_counters
    report_path = output_directory / f"hil-runtime-report-{ITERATION}.json"
    result_path = output_directory / f"hil-runtime-observation-{ITERATION}.json"
    from multidetect.runtime import write_runtime_run_report

    write_runtime_run_report(report_path, report)
    write_runtime_observation_result(result_path, observation, include_hash=True)
    binding = verify_runtime_report_binding(
        result_path,
        report_path,
        expected_runtime_mode=RuntimeMode.HIL.value,
    )
    loaded = load_runtime_observation_result(result_path)

    telemetry.application_messages_transmitted = 1
    telemetry.flight_control_messages_transmitted = 1
    telemetry.is_read_only = False
    telemetry.hardware_control_enabled = True
    telemetry.flight_control_enabled = True
    telemetry.output_locked = False
    try:
        runner.build_runtime_observation_result()
    except RuntimeError as exc:
        negative_rejection = str(exc)
    else:  # pragma: no cover - the guard is the subject of this evidence.
        negative_rejection = None

    return {
        "iteration": ITERATION,
        "release_label": PRODUCT_VERSION.release_label,
        "producer": "LiveMissionRunner.from_runtime_backend",
        "runtime_mode": RuntimeMode.HIL.value,
        "run_result": {
            "processed_frames": run_result.processed_frames,
            "stream_exhausted": report.stream_exhausted,
            "max_frames": report.max_frames,
            "processed_frame_ids": list(report.processed_frame_ids),
        },
        "runtime_message_counters": runtime_message_counters,
        "positive_pair": {
            "result": _summary(result_path),
            "runtime_report": _summary(report_path),
            "schema_errors": _schema_errors(result_path, schema_path),
            "round_trip": loaded == observation,
            "binding_gate_passed": binding["gate_passed"],
        },
        "negative_application_output_rejection": {
            "rejected": negative_rejection is not None,
            "message": negative_rejection,
        },
        "claim_boundary": {
            "software_runtime_identity_only": True,
            "hardware_acceptance": "not_claimed",
            "hil_acceptance": "not_claimed",
            "real_acceptance": "not_claimed",
            "flight_acceptance": "not_claimed",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument(
        "--schema",
        type=Path,
        default=Path("configs/schemas/runtime-observation-result.schema.json"),
    )
    args = parser.parse_args()
    summary = build_evidence(args.output_directory, args.schema)
    _write_json(args.output_directory / f"summary-{ITERATION}.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    positive = summary["positive_pair"]
    negative = summary["negative_application_output_rejection"]
    passed = (
        positive["schema_errors"] == 0
        and positive["round_trip"] is True
        and positive["binding_gate_passed"] is True
        and negative["rejected"] is True
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
