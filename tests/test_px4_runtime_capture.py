from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from multidetect import px4_runtime_capture as capture_module
from multidetect.autonomy.contracts import (
    ExecutableMission,
    MissionCommand,
    MissionFrame,
    MissionItem,
)
from multidetect.compat import UTC
from multidetect.live_runtime_observation_mission_binding import (
    verify_runtime_endpoint_vehicle_identity_sidecar,
)
from multidetect.mission_planning import load_executable_mission_document
from multidetect.pixhawk import (
    PixhawkAutopilotVersionIdentity,
    PixhawkHeartbeatIdentity,
    PixhawkReadOnlyConfig,
)
from multidetect.px4_mission import PX4_FIXED_WING_ADAPTER_HASH, PX4_FIXED_WING_ADAPTER_ID
from multidetect.px4_mission_feedback import Px4MissionFeedbackConfig, Px4MissionFeedbackTracker
from multidetect.px4_runtime_capture import (
    SCHEMA,
    capture_px4_runtime_sidecars,
    main,
)
from multidetect.runtime import RuntimeMode, RuntimeProfile, RuntimeRunReport
from multidetect.runtime_launcher import build_pixhawk_runtime_identity
from multidetect.runtime_observation import (
    RuntimeObservationResult,
    write_runtime_observation_result,
)
from multidetect.runtime_profiles import sign_runtime_profile_document

ROOT = Path(__file__).resolve().parents[1]
KEY = b"px4-runtime-capture-test-key-material-32-bytes"
KEY_ID = "px4-runtime-capture-test-key"


def _mission() -> ExecutableMission:
    return ExecutableMission(
        mission_id="px4-runtime-capture-test",
        platform_plan_hash="a" * 64,
        airframe_profile_hash="b" * 64,
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


def _heartbeat() -> PixhawkHeartbeatIdentity:
    return PixhawkHeartbeatIdentity(
        source_system_id=1,
        source_component_id=1,
        autopilot_id=12,
        vehicle_type_id=1,
        system_status_id=4,
        mavlink_version=3,
    )


def _autopilot() -> PixhawkAutopilotVersionIdentity:
    return PixhawkAutopilotVersionIdentity(
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


class _Provider:
    def __init__(
        self,
        config: PixhawkReadOnlyConfig,
        *,
        mission_feedback_tracker: Px4MissionFeedbackTracker,
        application_messages_transmitted: int = 0,
    ) -> None:
        self.config = config
        self.resolved_endpoint = config.endpoint
        self.heartbeat_identity = _heartbeat()
        self.autopilot_version_identity = _autopilot()
        self.autopilot_version_observed_at_s = 10.0
        self.is_read_only = True
        self.messages_received = 0
        self.messages_transmitted = 2
        self.telemetry_request_messages_transmitted = 2 - application_messages_transmitted
        self.application_messages_transmitted = application_messages_transmitted
        self.flight_control_messages_transmitted = 0
        self.message_type_counts: dict[str, int] = {}
        self._tracker = mission_feedback_tracker
        self._emitted = False
        self.closed = False

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

    def transport_link_healthy(self, *, now_s: float) -> bool:
        return True

    def close(self) -> None:
        self.closed = True


def _write_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, object]:
    base = tmp_path / "capture"
    base.mkdir()
    mission_path = _write_json(base / "mission.json", _mission().to_mapping())
    mission = load_executable_mission_document(mission_path)
    report = RuntimeRunReport(
        descriptor_hash="c" * 64,
        input_hash="d" * 64,
        processed_frame_ids=("hil:capture:0001",),
        started_at_s=10.0,
        finished_at_s=10.1,
        stream_exhausted=False,
        max_frames=1,
    )
    live_path = write_runtime_observation_result(
        base / "live" / "observation.json",
        RuntimeObservationResult(RuntimeMode.HIL, report),
    )
    config = PixhawkReadOnlyConfig(endpoint="udpin:127.0.0.1:14999", expected_system_id=1)
    provider = _Provider(
        config,
        mission_feedback_tracker=Px4MissionFeedbackTracker(
            mission,
            config=Px4MissionFeedbackConfig(expected_system_id=1, expected_component_id=1),
        ),
    )
    identity = build_pixhawk_runtime_identity(
        provider, airframe_profile_hash=mission.airframe_profile_hash
    )
    profile = RuntimeProfile.hil(
        vehicle_identity_hash=identity.vehicle_identity_hash,
        firmware_identity_hash=identity.firmware_identity_hash,
        airframe_profile_hash=mission.airframe_profile_hash,
        endpoint_binding_hash=identity.endpoint_binding_hash,
    )
    profile_path = _write_json(
        base / "profile.json",
        sign_runtime_profile_document(profile, hmac_key=KEY, key_id=KEY_ID),
    )
    return base, mission_path, live_path, profile_path


def test_capture_writes_sidecars_and_reruns_join(tmp_path: Path) -> None:
    base, mission_path, live_path, profile_path = _fixture(tmp_path)

    def factory(config, *, mission_feedback_tracker):
        return _Provider(config, mission_feedback_tracker=mission_feedback_tracker)

    summary = capture_px4_runtime_sidecars(
        base_directory=base,
        mission=mission_path,
        runtime_observation=live_path,
        runtime_profile=profile_path,
        runtime_mode="hil",
        endpoint="udpin:127.0.0.1:14999",
        runtime_profile_hmac_key=KEY,
        runtime_profile_key_id=KEY_ID,
        duration_seconds=0.0,
        clock=lambda: 10.0,
        observed_at=lambda: datetime(2026, 9, 7, 12, 0, tzinfo=UTC),
        provider_factory=factory,
    )

    assert summary["schema"] == SCHEMA
    assert summary["mission_feedback"]["gate_passed"] is True
    assert summary["join"]["gate_passed"] is True, summary["join"]["reasons"]
    assert summary["transport"]["telemetry_request_messages_transmitted"] == 2
    assert summary["transport"]["application_messages_transmitted"] == 0
    identity_path = Path(summary["endpoint_vehicle_identity"]["path"])
    verify_runtime_endpoint_vehicle_identity_sidecar(
        json.loads(identity_path.read_text(encoding="utf-8"))
    )

    schema = json.loads(
        (ROOT / "configs/schemas/px4-runtime-capture.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(summary)


def test_capture_keeps_application_output_fail_closed(tmp_path: Path) -> None:
    base, mission_path, live_path, profile_path = _fixture(tmp_path)

    def factory(config, *, mission_feedback_tracker):
        return _Provider(
            config,
            mission_feedback_tracker=mission_feedback_tracker,
            application_messages_transmitted=1,
        )

    summary = capture_px4_runtime_sidecars(
        base_directory=base,
        mission=mission_path,
        runtime_observation=live_path,
        runtime_profile=profile_path,
        runtime_mode="hil",
        endpoint="udpin:127.0.0.1:14999",
        runtime_profile_hmac_key=KEY,
        runtime_profile_key_id=KEY_ID,
        duration_seconds=0.0,
        clock=lambda: 10.0,
        observed_at=lambda: datetime(2026, 9, 7, 12, 0, tzinfo=UTC),
        provider_factory=factory,
    )

    assert summary["mission_feedback"]["gate_passed"] is False
    assert summary["join"]["gate_passed"] is False
    assert summary["transport"]["application_messages_transmitted"] == 1
    assert "one or more joined sources are not output locked" in summary["join"]["reasons"]


def test_capture_rejects_sources_outside_base(tmp_path: Path) -> None:
    base, mission_path, live_path, profile_path = _fixture(tmp_path)
    outside = tmp_path / "outside-mission.json"
    _write_json(outside, _mission().to_mapping())
    with pytest.raises(ValueError, match="inside base directory"):
        capture_px4_runtime_sidecars(
            base_directory=base,
            mission=outside,
            runtime_observation=live_path,
            runtime_profile=profile_path,
            runtime_mode="hil",
            endpoint="udpin:127.0.0.1:14999",
            runtime_profile_hmac_key=KEY,
            runtime_profile_key_id=KEY_ID,
            duration_seconds=0.0,
            clock=lambda: 10.0,
            provider_factory=lambda config, *, mission_feedback_tracker: _Provider(
                config,
                mission_feedback_tracker=mission_feedback_tracker,
            ),
        )


def test_capture_cli_reads_secret_only_from_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base, mission_path, live_path, profile_path = _fixture(tmp_path)
    monkeypatch.setattr(capture_module, "PixhawkReadOnlyTelemetryProvider", _Provider)
    monkeypatch.setenv("PX4_CAPTURE_PROFILE_KEY", KEY.decode("utf-8"))

    assert (
        main(
            [
                "--base-directory",
                str(base),
                "--mission",
                str(mission_path),
                "--runtime-observation",
                str(live_path),
                "--runtime-profile",
                str(profile_path),
                "--runtime-mode",
                "hil",
                "--endpoint",
                "udpin:127.0.0.1:14999",
                "--runtime-profile-hmac-key-env",
                "PX4_CAPTURE_PROFILE_KEY",
                "--runtime-profile-key-id",
                KEY_ID,
                "--duration-seconds",
                "0",
            ]
        )
        == 0
    )
    summary = json.loads(capsys.readouterr().out)
    assert summary["event"] == "px4_runtime_capture_completed"
    assert summary["join"]["gate_passed"] is True
