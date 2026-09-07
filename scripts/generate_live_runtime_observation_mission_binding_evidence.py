"""Generate deterministic v190 live/PX4 Mission sidecar-join evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

from jsonschema import Draft202012Validator

if __package__ in {None, ""}:
    source_root = Path(__file__).resolve().parents[1] / "src"
    if source_root.is_dir() and str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))

from multidetect.autonomy.contracts import (
    ExecutableMission,
    MissionCommand,
    MissionFrame,
    MissionItem,
)
from multidetect.autonomy.serialization import canonical_sha256
from multidetect.clock import ManualClock
from multidetect.compat import UTC
from multidetect.config import MissionConfig
from multidetect.domain import BoundingBox, Detection, SensorKind, VehicleTelemetry
from multidetect.live import LiveMissionRunner, LiveRunConfig
from multidetect.live_runtime_observation_mission_binding import (
    build_runtime_endpoint_vehicle_identity_sidecar,
    verify_live_runtime_observation_mission_binding,
)
from multidetect.mission import MissionController
from multidetect.pixhawk import (
    PixhawkAutopilotVersionIdentity,
    PixhawkHeartbeatIdentity,
    PixhawkReadOnlyConfig,
)
from multidetect.product_version import PRODUCT_VERSION
from multidetect.px4_mission import PX4_FIXED_WING_ADAPTER_HASH, PX4_FIXED_WING_ADAPTER_ID
from multidetect.px4_mission_feedback import Px4MissionFeedbackConfig, Px4MissionFeedbackTracker
from multidetect.px4_mission_feedback_cli import observe_px4_mission_feedback
from multidetect.runtime import RuntimeDescriptor, RuntimeMode, RuntimeProfile
from multidetect.runtime_launcher import build_pixhawk_runtime_identity
from multidetect.runtime_observation import (
    write_runtime_observation_result,
)
from multidetect.runtime_profiles import sign_runtime_profile_document

ITERATION = "v190"
PROFILE_KEY = b"multidetect-v190-live-mission-binding-demo-key-material"
PROFILE_KEY_ID = "multidetect-v190-demo-key"


class _FrameSource:
    def __init__(self, clock: ManualClock) -> None:
        self.clock = clock
        self.next_index = 0

    def open(self) -> None:
        return None

    def read(self):
        if self.next_index >= 2:
            raise StopIteration
        self.next_index += 1
        captured_at_s = 100.0 + (self.next_index - 1) * 0.1
        self.clock.advance_to(captured_at_s)
        from multidetect.vision import CapturedFrame

        return CapturedFrame(
            frame_id=f"v190-live-{self.next_index:04d}",
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
                "v190-live-mission-binding",
            ),
        )

    def covers_labels(self, _labels: object) -> bool:
        return False


class _Telemetry:
    telemetry_request_messages_transmitted = 2
    messages_transmitted = 2
    application_messages_transmitted = 0
    flight_control_messages_transmitted = 0
    is_read_only = True
    hardware_control_enabled = False
    flight_control_enabled = False
    output_locked = True

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


class _LiveBackend:
    def __init__(self, *, clock: ManualClock, profile: RuntimeProfile, airframe_hash: str) -> None:
        self.clock = clock
        self.frame_source = _FrameSource(clock)
        self.telemetry_provider = _Telemetry()
        self.descriptor = RuntimeDescriptor(
            mode=RuntimeMode.HIL,
            profile=profile,
            backend_id="evidence:v190-live-mission-binding",
            airframe_profile_hash=airframe_hash,
            scenario_id="v190-live-mission-binding",
            scenario_manifest_hash="4" * 64,
            scenario_seed=190,
            deterministic=False,
        )


class _FeedbackProvider:
    def __init__(self, mission: ExecutableMission) -> None:
        self.config = PixhawkReadOnlyConfig(endpoint="udpin:127.0.0.1:14999", expected_system_id=1)
        self.resolved_endpoint = self.config.endpoint
        self.heartbeat_identity = PixhawkHeartbeatIdentity(
            source_system_id=1,
            source_component_id=1,
            autopilot_id=12,
            vehicle_type_id=1,
            system_status_id=4,
            mavlink_version=3,
        )
        self.autopilot_version_identity = PixhawkAutopilotVersionIdentity(
            source_system_id=1,
            source_component_id=1,
            capabilities=(1 << 63) | 17,
            flight_sw_version=0x010203FF,
            middleware_sw_version=0x040506C0,
            os_sw_version=0x07080940,
            board_version=0x10203040,
            flight_custom_version_hex="0102030405060708",
            middleware_custom_version_hex="1112131415161718",
            os_custom_version_hex="2122232425262728",
            vendor_id=9900,
            product_id=42,
            device_uid_source="none",
            device_uid_hash=None,
        )
        self.autopilot_version_observed_at_s = 10.0
        self.is_read_only = True
        self.messages_received = 0
        self.messages_transmitted = 0
        self.message_type_counts: dict[str, int] = {}
        self._emitted = False
        self._tracker = Px4MissionFeedbackTracker(
            mission,
            config=Px4MissionFeedbackConfig(expected_system_id=1, expected_component_id=1),
        )

    def snapshot(self, *, now_s: float) -> object:
        if not self._emitted:
            for message_type in ("MISSION_CURRENT", "MISSION_ITEM_REACHED"):
                assert self._tracker.ingest_message(
                    {
                        "message_type": message_type,
                        "seq": 0,
                        "source_system_id": 1,
                        "source_component_id": 1,
                    },
                    observed_at_s=now_s,
                )
                self.messages_received += 1
                self.message_type_counts[message_type] = 1
            self._emitted = True
        return object()

    def mission_feedback_snapshot(self, *, now_s: float):
        return self._tracker.snapshot(now_s=now_s)


def _mission() -> ExecutableMission:
    return ExecutableMission(
        mission_id="v190-live-mission-binding",
        platform_plan_hash="b" * 64,
        airframe_profile_hash=hashlib.sha256(b"v190-fixed-wing").hexdigest(),
        airframe_adapter_id=PX4_FIXED_WING_ADAPTER_ID,
        airframe_adapter_hash=PX4_FIXED_WING_ADAPTER_HASH,
        items=(
            MissionItem(
                sequence=0,
                command=MissionCommand.LAND,
                frame=MissionFrame.GLOBAL_RELATIVE_ALT_INT,
                latitude_deg=1.3521,
                longitude_deg=103.8198,
                altitude_m=0.0,
            ),
        ),
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
    return {"path": path.name, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}


def _schema_errors(path: Path, schema_path: Path) -> int:
    document = json.loads(path.read_text(encoding="utf-8"))
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return sum(1 for _ in Draft202012Validator(schema).iter_errors(document))


def build_evidence(
    output_directory: Path,
    *,
    join_schema_path: Path,
    identity_schema_path: Path,
) -> dict[str, object]:
    output_directory = output_directory.expanduser().resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    mission = _mission()
    mission_path = output_directory / "mission" / "v190.json"
    _write_json(mission_path, mission.to_mapping())

    feedback_provider = _FeedbackProvider(mission)
    observed_identity = build_pixhawk_runtime_identity(
        feedback_provider,
        airframe_profile_hash=mission.airframe_profile_hash,
    )
    profile = RuntimeProfile.hil(
        vehicle_identity_hash=observed_identity.vehicle_identity_hash,
        firmware_identity_hash=observed_identity.firmware_identity_hash,
        airframe_profile_hash=mission.airframe_profile_hash,
        endpoint_binding_hash=observed_identity.endpoint_binding_hash,
    )
    profile_path = output_directory / "profile" / "hil.json"
    _write_json(
        profile_path,
        sign_runtime_profile_document(profile, hmac_key=PROFILE_KEY, key_id=PROFILE_KEY_ID),
    )

    feedback = observe_px4_mission_feedback(
        mission,
        feedback_provider,
        runtime_mode="hil",
        duration_s=0.0,
        clock=lambda: 10.0,
        observed_at=lambda: datetime(2026, 9, 7, 12, 0, tzinfo=UTC),
        runtime_profile_path=profile_path,
        runtime_profile_hmac_key=PROFILE_KEY,
        runtime_profile_key_id=PROFILE_KEY_ID,
    )
    feedback_path = output_directory / "feedback" / "hil.json"
    _write_json(feedback_path, feedback)

    clock = ManualClock(initial_monotonic_s=100.0)
    live_backend = _LiveBackend(
        clock=clock,
        profile=profile,
        airframe_hash=mission.airframe_profile_hash,
    )
    runner = LiveMissionRunner.from_runtime_backend(
        backend=live_backend,
        mission=MissionController(
            MissionConfig.from_json(
                Path(__file__).resolve().parents[1] / "configs/missions/fire_patrol.demo.json"
            )
        ),
        detector=_Detector(),
        config=LiveRunConfig(max_frames=2, display=False),
    )
    run_result = runner.run()
    observation = runner.build_runtime_observation_result()
    live_result_path = output_directory / "live" / "observation.json"
    live_report_path = output_directory / "live" / "runtime-report.json"
    write_runtime_observation_result(live_result_path, observation, include_hash=True)
    from multidetect.runtime import write_runtime_run_report

    write_runtime_run_report(live_report_path, observation.runtime_run_report)

    identity_path = output_directory / "identity" / "hil.json"
    identity_sidecar = build_runtime_endpoint_vehicle_identity_sidecar(
        runtime_mode="hil",
        runtime_descriptor_hash=observation.runtime_run_report.descriptor_hash,
        runtime_input_hash=observation.runtime_run_report.input_hash,
        runtime_profile_document_sha256=hashlib.sha256(profile_path.read_bytes()).hexdigest(),
        endpoint={
            "configured_endpoint": feedback_provider.config.endpoint,
            "resolved_endpoint": feedback_provider.resolved_endpoint,
            "baud": feedback_provider.config.baud,
        },
        heartbeat_identity=feedback_provider.heartbeat_identity,
        observed_identity=observed_identity,
        observed_at_utc="2026-09-07T12:00:00Z",
    )
    _write_json(identity_path, identity_sidecar)

    joined = verify_live_runtime_observation_mission_binding(
        live_result_path,
        feedback_path,
        profile_path,
        identity_path,
        mission=mission_path,
        base_directory=output_directory,
        hmac_key=PROFILE_KEY,
        expected_key_id=PROFILE_KEY_ID,
    )
    joined_path = output_directory / "live-runtime-observation-mission-binding-v190.json"
    _write_json(joined_path, joined)

    tampered_identity = dict(identity_sidecar)
    tampered_identity["runtime_mode"] = "real"
    tampered_identity["identity_sha256"] = canonical_sha256(
        {key: value for key, value in tampered_identity.items() if key != "identity_sha256"}
    )
    tampered_identity_path = output_directory / "identity" / "tampered-mode.json"
    _write_json(tampered_identity_path, tampered_identity)
    negative = verify_live_runtime_observation_mission_binding(
        live_result_path,
        feedback_path,
        profile_path,
        tampered_identity_path,
        mission=mission_path,
        base_directory=output_directory,
        hmac_key=PROFILE_KEY,
        expected_key_id=PROFILE_KEY_ID,
    )

    summary = {
        "iteration": ITERATION,
        "release_label": PRODUCT_VERSION.release_label,
        "producer": "LiveMissionRunner + PX4 feedback + signed profile + identity sidecar",
        "runtime_mode": "hil",
        "positive_join": {
            "path": joined_path.name,
            "bytes": joined_path.stat().st_size,
            "sha256": hashlib.sha256(joined_path.read_bytes()).hexdigest(),
            "gate_passed": joined["gate_passed"],
            "schema_errors": _schema_errors(joined_path, join_schema_path),
            "checks": joined["checks"],
        },
        "sources": {
            "live_observation": _summary(live_result_path),
            "runtime_report": _summary(live_report_path),
            "mission": _summary(mission_path),
            "mission_feedback": _summary(feedback_path),
            "runtime_profile": _summary(profile_path),
            "endpoint_vehicle_identity": {
                **_summary(identity_path),
                "schema_errors": _schema_errors(identity_path, identity_schema_path),
            },
        },
        "run_result": {
            "processed_frames": run_result.processed_frames,
            "processed_frame_ids": list(observation.runtime_run_report.processed_frame_ids),
            "termination_reason": observation.runtime_run_report.termination_reason,
        },
        "negative_mode_drift": {
            "rejected": negative["gate_passed"] is False,
            "event": negative["event"],
            "reasons": negative["reasons"],
        },
        "claim_boundary": joined["claim_boundary"],
    }
    _write_json(output_directory / "summary-v190.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument(
        "--join-schema",
        type=Path,
        default=Path("configs/schemas/live-runtime-observation-mission-binding.schema.json"),
    )
    parser.add_argument(
        "--identity-schema",
        type=Path,
        default=Path("configs/schemas/runtime-endpoint-vehicle-identity.schema.json"),
    )
    args = parser.parse_args()
    summary = build_evidence(
        args.output_directory,
        join_schema_path=args.join_schema,
        identity_schema_path=args.identity_schema,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    positive = summary["positive_join"]
    identity = summary["sources"]["endpoint_vehicle_identity"]
    negative = summary["negative_mode_drift"]
    passed = (
        positive["gate_passed"] is True
        and positive["schema_errors"] == 0
        and identity["schema_errors"] == 0
        and negative["rejected"] is True
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
