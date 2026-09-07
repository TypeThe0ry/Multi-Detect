"""Generate deterministic v191 PX4 runtime sidecar-capture evidence.

The harness uses the production ``capture_px4_runtime_sidecars`` entry point
with a bounded in-memory provider.  It exercises both the receive-only path
and the fail-closed application-output path; it never opens a serial link or
claims a physical HIL/REAL run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

from jsonschema import Draft202012Validator

if __package__ in {None, ""}:
    _repository_source = Path(__file__).resolve().parents[1] / "src"
    if _repository_source.is_dir() and str(_repository_source) not in sys.path:
        sys.path.insert(0, str(_repository_source))

from multidetect.autonomy.contracts import (
    ExecutableMission,
    MissionCommand,
    MissionFrame,
    MissionItem,
)
from multidetect.compat import UTC
from multidetect.mission_planning import load_executable_mission_document
from multidetect.pixhawk import (
    PIXHAWK_AUTOPILOT_IDS,
    PixhawkAutopilotVersionIdentity,
    PixhawkHeartbeatIdentity,
    PixhawkReadOnlyConfig,
)
from multidetect.product_version import PRODUCT_VERSION
from multidetect.px4_mission import PX4_FIXED_WING_ADAPTER_HASH, PX4_FIXED_WING_ADAPTER_ID
from multidetect.px4_mission_feedback import Px4MissionFeedbackConfig, Px4MissionFeedbackTracker
from multidetect.px4_runtime_capture import capture_px4_runtime_sidecars
from multidetect.runtime import RuntimeMode, RuntimeProfile, RuntimeRunReport
from multidetect.runtime_launcher import build_pixhawk_runtime_identity
from multidetect.runtime_observation import (
    RuntimeObservationResult,
    write_runtime_observation_result,
)
from multidetect.runtime_profiles import sign_runtime_profile_document

ITERATION = "v191"
PROFILE_KEY = b"multidetect-v191-px4-runtime-capture-demo-key-material"
PROFILE_KEY_ID = "multidetect-v191-demo-key"
ENDPOINT = "udpin:127.0.0.1:14999"


class _Provider:
    """Deterministic provider double with explicit output buckets."""

    def __init__(
        self,
        config: PixhawkReadOnlyConfig,
        *,
        mission_feedback_tracker: Px4MissionFeedbackTracker,
        application_messages_transmitted: int = 0,
    ) -> None:
        self.config = config
        self.resolved_endpoint = config.endpoint
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
        self.is_read_only = application_messages_transmitted == 0
        self.hardware_control_enabled = application_messages_transmitted != 0
        self.flight_control_enabled = application_messages_transmitted != 0
        self.output_locked = application_messages_transmitted == 0
        self.messages_received = 0
        self.telemetry_request_messages_transmitted = 2
        self.application_messages_transmitted = application_messages_transmitted
        self.flight_control_messages_transmitted = 0
        self.messages_transmitted = (
            self.telemetry_request_messages_transmitted
            + self.application_messages_transmitted
            + self.flight_control_messages_transmitted
        )
        self.message_type_counts: dict[str, int] = {}
        self._tracker = mission_feedback_tracker
        self._emitted = False

    def snapshot(self, *, now_s: float) -> object:
        if not self._emitted:
            for message_type in ("MISSION_CURRENT", "MISSION_ITEM_REACHED"):
                accepted = self._tracker.ingest_message(
                    {
                        "message_type": message_type,
                        "seq": 0,
                        "source_system_id": 1,
                        "source_component_id": 1,
                    },
                    observed_at_s=now_s,
                )
                if not accepted:  # pragma: no cover - fixture contract guard.
                    raise RuntimeError(f"fixture feedback rejected {message_type}")
                self.messages_received += 1
                self.message_type_counts[message_type] = 1
            self._emitted = True
        return object()

    def mission_feedback_snapshot(self, *, now_s: float):
        return self._tracker.snapshot(now_s=now_s)

    def transport_link_healthy(self, *, now_s: float) -> bool:
        del now_s
        return True

    def close(self) -> None:
        return None


def _mission() -> ExecutableMission:
    return ExecutableMission(
        mission_id="v191-px4-runtime-capture",
        platform_plan_hash="a" * 64,
        airframe_profile_hash=hashlib.sha256(b"v191-fixed-wing").hexdigest(),
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


def _prepare_inputs(output_directory: Path) -> tuple[Path, Path, Path]:
    mission_path = output_directory / "inputs" / "mission.json"
    mission = _mission()
    _write_json(mission_path, mission.to_mapping())
    loaded_mission = load_executable_mission_document(mission_path)

    report = RuntimeRunReport(
        descriptor_hash="c" * 64,
        input_hash="d" * 64,
        processed_frame_ids=(f"hil:{ITERATION}:0001", f"hil:{ITERATION}:0002"),
        started_at_s=10.0,
        finished_at_s=10.1,
        stream_exhausted=False,
        max_frames=2,
    )
    live_path = output_directory / "inputs" / "runtime-observation.json"
    write_runtime_observation_result(
        live_path,
        RuntimeObservationResult(RuntimeMode.HIL, report),
        include_hash=True,
    )

    identity_provider = _Provider(
        PixhawkReadOnlyConfig(
            endpoint=ENDPOINT,
            expected_system_id=1,
            expected_autopilot_id=PIXHAWK_AUTOPILOT_IDS["px4"],
        ),
        mission_feedback_tracker=Px4MissionFeedbackTracker(
            loaded_mission,
            config=Px4MissionFeedbackConfig(expected_system_id=1, expected_component_id=1),
        ),
    )
    identity = build_pixhawk_runtime_identity(
        identity_provider,
        airframe_profile_hash=loaded_mission.airframe_profile_hash,
    )
    profile = RuntimeProfile.hil(
        vehicle_identity_hash=identity.vehicle_identity_hash,
        firmware_identity_hash=identity.firmware_identity_hash,
        airframe_profile_hash=loaded_mission.airframe_profile_hash,
        endpoint_binding_hash=identity.endpoint_binding_hash,
    )
    profile_path = output_directory / "inputs" / "runtime-profile.json"
    _write_json(
        profile_path,
        sign_runtime_profile_document(profile, hmac_key=PROFILE_KEY, key_id=PROFILE_KEY_ID),
    )
    return mission_path, live_path, profile_path


def _capture_case(output_directory: Path, *, application_output: int) -> dict[str, object]:
    mission_path, live_path, profile_path = _prepare_inputs(output_directory)

    def factory(config, *, mission_feedback_tracker):
        return _Provider(
            config,
            mission_feedback_tracker=mission_feedback_tracker,
            application_messages_transmitted=application_output,
        )

    return capture_px4_runtime_sidecars(
        base_directory=output_directory,
        mission=mission_path,
        runtime_observation=live_path,
        runtime_profile=profile_path,
        runtime_mode="hil",
        endpoint=ENDPOINT,
        runtime_profile_hmac_key=PROFILE_KEY,
        runtime_profile_key_id=PROFILE_KEY_ID,
        duration_seconds=0.0,
        clock=lambda: 10.0,
        observed_at=lambda: datetime(2026, 9, 7, 12, 0, tzinfo=UTC),
        provider_factory=factory,
        overwrite=True,
    )


def build_evidence(
    output_directory: Path,
    *,
    capture_schema_path: Path,
    join_schema_path: Path,
    identity_schema_path: Path,
) -> dict[str, object]:
    output_directory = output_directory.expanduser().resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    positive_dir = output_directory / "positive"
    negative_dir = output_directory / "negative-application-output"
    positive = _capture_case(positive_dir, application_output=0)
    negative = _capture_case(negative_dir, application_output=1)

    def sidecar_checks(case_dir: Path, summary: dict[str, object]) -> dict[str, object]:
        join_path = Path(summary["join"]["path"])
        identity_path = Path(summary["endpoint_vehicle_identity"]["path"])
        feedback_path = Path(summary["mission_feedback"]["path"])
        capture_summary = {
            "schema_errors": _schema_errors(
                case_dir / "capture-summary.json",
                capture_schema_path,
            ),
            "join_schema_errors": _schema_errors(
                join_path,
                join_schema_path,
            ),
            "identity_schema_errors": _schema_errors(
                identity_path,
                identity_schema_path,
            ),
            "feedback_path": _summary(feedback_path),
            "identity_path": _summary(identity_path),
            "join_path": _summary(join_path),
        }
        return capture_summary

    positive_capture_path = positive_dir / "capture-summary.json"
    negative_capture_path = negative_dir / "capture-summary.json"
    _write_json(positive_capture_path, positive)
    _write_json(negative_capture_path, negative)
    positive_checks = sidecar_checks(positive_dir, positive)
    negative_checks = sidecar_checks(negative_dir, negative)
    summary = {
        "iteration": ITERATION,
        "release_label": PRODUCT_VERSION.release_label,
        "producer": "capture_px4_runtime_sidecars",
        "runtime_mode": "hil",
        "positive_receive_only": {
            **positive_checks,
            "join_gate_passed": positive["join"]["gate_passed"],
            "feedback_gate_passed": positive["mission_feedback"]["gate_passed"],
            "transport": positive["transport"],
        },
        "negative_application_output": {
            **negative_checks,
            "join_gate_passed": negative["join"]["gate_passed"],
            "feedback_gate_passed": negative["mission_feedback"]["gate_passed"],
            "rejected": (
                negative["join"]["gate_passed"] is False
                and negative["mission_feedback"]["gate_passed"] is False
            ),
            "reasons": negative["join"]["reasons"],
            "transport": negative["transport"],
        },
        "claim_boundary": {
            "software_sidecar_capture": True,
            "hardware_acceptance": "not_claimed",
            "hil_acceptance": "not_claimed",
            "real_acceptance": "not_claimed",
            "flight_acceptance": "not_claimed",
            "operator_visual_acceptance": "not_claimed",
        },
    }
    _write_json(output_directory / f"summary-{ITERATION}.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument(
        "--capture-schema",
        type=Path,
        default=Path("configs/schemas/px4-runtime-capture.schema.json"),
    )
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
        capture_schema_path=args.capture_schema,
        join_schema_path=args.join_schema,
        identity_schema_path=args.identity_schema,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    positive = summary["positive_receive_only"]
    negative = summary["negative_application_output"]
    passed = (
        positive["schema_errors"] == 0
        and positive["join_schema_errors"] == 0
        and positive["identity_schema_errors"] == 0
        and positive["join_gate_passed"] is True
        and positive["feedback_gate_passed"] is True
        and positive["transport"]["telemetry_request_messages_transmitted"] == 2
        and positive["transport"]["application_messages_transmitted"] == 0
        and negative["rejected"] is True
        and negative["schema_errors"] == 0
        and negative["join_schema_errors"] == 0
        and negative["identity_schema_errors"] == 0
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
