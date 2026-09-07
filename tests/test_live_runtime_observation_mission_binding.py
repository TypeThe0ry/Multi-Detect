from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from multidetect.autonomy.contracts import (
    ExecutableMission,
    MissionCommand,
    MissionFrame,
    MissionItem,
)
from multidetect.compat import UTC
from multidetect.live_runtime_observation_mission_binding import (
    ERROR_EVENT,
    IDENTITY_SCHEMA,
    SCHEMA,
    build_runtime_endpoint_vehicle_identity_sidecar,
    main,
    verify_live_runtime_observation_mission_binding,
    verify_runtime_endpoint_vehicle_identity_sidecar,
)
from multidetect.pixhawk import (
    PixhawkAutopilotVersionIdentity,
    PixhawkHeartbeatIdentity,
    PixhawkReadOnlyConfig,
)
from multidetect.px4_mission import PX4_FIXED_WING_ADAPTER_HASH, PX4_FIXED_WING_ADAPTER_ID
from multidetect.px4_mission_feedback import Px4MissionFeedbackConfig, Px4MissionFeedbackTracker
from multidetect.px4_mission_feedback_cli import observe_px4_mission_feedback
from multidetect.runtime import RuntimeMode, RuntimeProfile, RuntimeRunReport
from multidetect.runtime_launcher import (
    build_pixhawk_runtime_identity,
    derive_endpoint_binding_hash,
)
from multidetect.runtime_observation import (
    RuntimeObservationResult,
    write_runtime_observation_result,
)
from multidetect.runtime_profiles import sign_runtime_profile_document

ROOT = Path(__file__).resolve().parents[1]
JOIN_SCHEMA_PATH = ROOT / "configs/schemas/live-runtime-observation-mission-binding.schema.json"
IDENTITY_SCHEMA_PATH = ROOT / "configs/schemas/runtime-endpoint-vehicle-identity.schema.json"
PROFILE_KEY = b"live-runtime-observation-mission-binding-test-key-material"
PROFILE_KEY_ID = "live-runtime-observation-test-key-v1"


def _write_json(path: Path, document: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _mission() -> ExecutableMission:
    return ExecutableMission(
        mission_id="live-observation-mission-binding",
        platform_plan_hash="b" * 64,
        airframe_profile_hash=hashlib.sha256(b"live-observation-fixed-wing").hexdigest(),
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


class _Provider:
    def __init__(self, mission: ExecutableMission) -> None:
        self.config = PixhawkReadOnlyConfig(endpoint="udpin:127.0.0.1:14899", expected_system_id=1)
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


def _runtime_result(tmp_path: Path) -> tuple[Path, str, str]:
    report = RuntimeRunReport(
        descriptor_hash="a" * 64,
        input_hash="b" * 64,
        processed_frame_ids=("hil:binding:0001", "hil:binding:0002"),
        started_at_s=10.0,
        finished_at_s=10.2,
        stream_exhausted=False,
        max_frames=2,
    )
    result = RuntimeObservationResult(RuntimeMode.HIL, report)
    path = write_runtime_observation_result(
        tmp_path / "live" / "observation.json",
        result,
        include_hash=True,
    )
    return path, report.descriptor_hash, report.input_hash


def _fixture(tmp_path: Path) -> dict[str, Path | str]:
    mission = _mission()
    mission_path = _write_json(tmp_path / "mission" / "mission.json", mission.to_mapping())
    provider = _Provider(mission)
    identity = build_pixhawk_runtime_identity(
        provider,
        airframe_profile_hash=mission.airframe_profile_hash,
    )
    profile = RuntimeProfile.hil(
        vehicle_identity_hash=identity.vehicle_identity_hash,
        firmware_identity_hash=identity.firmware_identity_hash,
        airframe_profile_hash=mission.airframe_profile_hash,
        endpoint_binding_hash=identity.endpoint_binding_hash,
    )
    profile_path = _write_json(
        tmp_path / "profile" / "hil.json",
        sign_runtime_profile_document(profile, hmac_key=PROFILE_KEY, key_id=PROFILE_KEY_ID),
    )
    feedback = observe_px4_mission_feedback(
        mission,
        provider,
        runtime_mode="hil",
        duration_s=0.0,
        clock=lambda: 10.0,
        observed_at=lambda: datetime(2026, 9, 7, 12, 0, tzinfo=UTC),
        runtime_profile_path=profile_path,
        runtime_profile_hmac_key=PROFILE_KEY,
        runtime_profile_key_id=PROFILE_KEY_ID,
    )
    feedback_path = _write_json(tmp_path / "feedback" / "hil.json", feedback)
    live_path, descriptor_hash, input_hash = _runtime_result(tmp_path)
    profile_document_sha256 = hashlib.sha256(profile_path.read_bytes()).hexdigest()
    identity_sidecar = build_runtime_endpoint_vehicle_identity_sidecar(
        runtime_mode="hil",
        runtime_descriptor_hash=descriptor_hash,
        runtime_input_hash=input_hash,
        runtime_profile_document_sha256=profile_document_sha256,
        endpoint={
            "configured_endpoint": provider.config.endpoint,
            "resolved_endpoint": provider.resolved_endpoint,
            "baud": provider.config.baud,
        },
        heartbeat_identity=provider.heartbeat_identity,
        observed_identity=identity,
        observed_at_utc="2026-09-07T12:00:00Z",
    )
    identity_path = _write_json(
        tmp_path / "identity" / "hil.json",
        identity_sidecar,
    )
    return {
        "root": tmp_path,
        "mission": mission_path,
        "live": live_path,
        "feedback": feedback_path,
        "profile": profile_path,
        "identity": identity_path,
    }


def test_identity_sidecar_round_trip_and_schema(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    raw = json.loads(Path(fixture["identity"]).read_text(encoding="utf-8"))
    assert raw["schema"] == IDENTITY_SCHEMA
    assert verify_runtime_endpoint_vehicle_identity_sidecar(raw) == raw
    schema = json.loads(IDENTITY_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(raw)


def test_join_binds_live_result_mission_feedback_profile_and_identity(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    document = verify_live_runtime_observation_mission_binding(
        fixture["live"],
        fixture["feedback"],
        fixture["profile"],
        fixture["identity"],
        mission=fixture["mission"],
        base_directory=fixture["root"],
        hmac_key=PROFILE_KEY,
        expected_key_id=PROFILE_KEY_ID,
    )
    assert document["schema"] == SCHEMA
    assert document["event"] == "live_runtime_observation_mission_binding_verified"
    assert document["gate_passed"] is True, document["reasons"]
    assert all(document["checks"].values())
    schema = json.loads(JOIN_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(document)
    assert document["claims"]["real_acceptance"] is False


def test_join_accepts_telemetry_request_traffic_when_outputs_stay_locked(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    feedback_path = Path(fixture["feedback"])
    feedback = json.loads(feedback_path.read_text(encoding="utf-8"))
    transport = feedback["transport"]
    transport.update(
        {
            "messages_transmitted": 2,
            "telemetry_request_messages_transmitted": 2,
            "application_messages_transmitted": 0,
            "flight_control_messages_transmitted": 0,
        }
    )
    from multidetect.autonomy.serialization import canonical_sha256

    feedback["evidence_sha256"] = canonical_sha256(
        {key: value for key, value in feedback.items() if key != "evidence_sha256"}
    )
    _write_json(feedback_path, feedback)

    document = verify_live_runtime_observation_mission_binding(
        fixture["live"],
        feedback_path,
        fixture["profile"],
        fixture["identity"],
        mission=fixture["mission"],
        base_directory=fixture["root"],
        hmac_key=PROFILE_KEY,
        expected_key_id=PROFILE_KEY_ID,
    )

    assert document["gate_passed"] is True, document["reasons"]
    assert document["checks"]["all_outputs_locked"] is True


def test_join_rejects_identity_endpoint_drift_even_with_recomputed_sidecar_hash(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    raw = json.loads(Path(fixture["identity"]).read_text(encoding="utf-8"))
    raw["endpoint"]["resolved_endpoint"] = "udpin:127.0.0.1:14999"
    raw["observed_identity"]["endpoint_binding_hash"] = derive_endpoint_binding_hash(
        PixhawkReadOnlyConfig(
            endpoint=raw["endpoint"]["configured_endpoint"],
            baud=raw["endpoint"]["baud"],
        ),
        resolved_endpoint=raw["endpoint"]["resolved_endpoint"],
    )
    raw["identity_sha256"] = "0" * 64
    from multidetect.autonomy.serialization import canonical_sha256

    raw["identity_sha256"] = canonical_sha256(
        {key: value for key, value in raw.items() if key != "identity_sha256"}
    )
    _write_json(Path(fixture["identity"]), raw)
    document = verify_live_runtime_observation_mission_binding(
        fixture["live"],
        fixture["feedback"],
        fixture["profile"],
        fixture["identity"],
        mission=fixture["mission"],
        base_directory=fixture["root"],
        hmac_key=PROFILE_KEY,
        expected_key_id=PROFILE_KEY_ID,
    )
    assert document["event"] == ERROR_EVENT
    assert document["gate_passed"] is False
    assert any("endpoint" in reason for reason in document["reasons"])


def test_join_rejects_runtime_hash_drift(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    raw = json.loads(Path(fixture["identity"]).read_text(encoding="utf-8"))
    raw["runtime_input_hash"] = "f" * 64
    from multidetect.autonomy.serialization import canonical_sha256

    raw["identity_sha256"] = canonical_sha256(
        {key: value for key, value in raw.items() if key != "identity_sha256"}
    )
    _write_json(Path(fixture["identity"]), raw)
    document = verify_live_runtime_observation_mission_binding(
        fixture["live"],
        fixture["feedback"],
        fixture["profile"],
        fixture["identity"],
        mission=fixture["mission"],
        base_directory=fixture["root"],
        hmac_key=PROFILE_KEY,
        expected_key_id=PROFILE_KEY_ID,
    )
    assert document["gate_passed"] is False
    assert document["checks"]["runtime_input_hash_matches"] is False
    assert any("runtime input hash" in reason for reason in document["reasons"])


def test_join_missing_hmac_key_fails_closed_with_schema_valid_error(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    document = verify_live_runtime_observation_mission_binding(
        fixture["live"],
        fixture["feedback"],
        fixture["profile"],
        fixture["identity"],
        mission=fixture["mission"],
        base_directory=fixture["root"],
        hmac_key=None,
        expected_key_id=PROFILE_KEY_ID,
    )
    assert document["gate_passed"] is False
    assert document["checks"]["runtime_profile_document_valid"] is False
    schema = json.loads(JOIN_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(document)


def test_cli_writes_verified_join_and_is_registered(tmp_path: Path, monkeypatch, capsys) -> None:
    fixture = _fixture(tmp_path)
    monkeypatch.setenv("V190_PROFILE_KEY", PROFILE_KEY.decode("utf-8"))
    output = tmp_path / "joined.json"
    assert (
        main(
            [
                "--runtime-observation",
                str(fixture["live"]),
                "--mission-feedback",
                str(fixture["feedback"]),
                "--runtime-profile",
                str(fixture["profile"]),
                "--endpoint-vehicle-identity",
                str(fixture["identity"]),
                "--mission",
                str(fixture["mission"]),
                "--base-directory",
                str(fixture["root"]),
                "--runtime-profile-hmac-key-env",
                "V190_PROFILE_KEY",
                "--runtime-profile-key-id",
                PROFILE_KEY_ID,
                "--out",
                str(output),
            ]
        )
        == 0
    )
    capsys.readouterr()
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["gate_passed"] is True
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "multidetect-live-runtime-observation-mission-bind" in pyproject


@pytest.mark.skipif(not hasattr(Path, "symlink_to"), reason="path symlink support unavailable")
def test_join_rejects_symlink_source(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    link = tmp_path / "identity" / "identity-link.json"
    try:
        link.symlink_to(Path(fixture["identity"]))
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links unavailable")
    document = verify_live_runtime_observation_mission_binding(
        fixture["live"],
        fixture["feedback"],
        fixture["profile"],
        link,
        mission=fixture["mission"],
        base_directory=fixture["root"],
        hmac_key=PROFILE_KEY,
        expected_key_id=PROFILE_KEY_ID,
    )
    assert document["gate_passed"] is False
    assert any("symbolic link" in reason for reason in document["reasons"])
