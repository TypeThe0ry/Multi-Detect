from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from jsonschema import Draft202012Validator

from multidetect.autonomy.contracts import (
    ExecutableMission,
    MissionCommand,
    MissionFrame,
    MissionItem,
)
from multidetect.compat import UTC
from multidetect.pixhawk import (
    PixhawkAutopilotVersionIdentity,
    PixhawkHeartbeatIdentity,
    PixhawkReadOnlyConfig,
)
from multidetect.px4_mission import (
    PX4_FIXED_WING_ADAPTER_HASH,
    PX4_FIXED_WING_ADAPTER_ID,
    PX4_MULTIROTOR_ADAPTER_HASH,
    PX4_MULTIROTOR_ADAPTER_ID,
    PX4_VTOL_ADAPTER_HASH,
    PX4_VTOL_ADAPTER_ID,
)
from multidetect.px4_mission_feedback import (
    Px4MissionFeedbackConfig,
    Px4MissionFeedbackTracker,
)
from multidetect.px4_mission_feedback_binding import (
    PX4_MISSION_FEEDBACK_REQUIRED_COMBINATIONS,
    build_px4_mission_feedback_binding,
)
from multidetect.px4_mission_feedback_cli import observe_px4_mission_feedback
from multidetect.px4_mission_feedback_profile_binding import (
    SCHEMA,
    main,
    verify_px4_mission_feedback_profile_binding,
)
from multidetect.runtime import RuntimeProfile
from multidetect.runtime_launcher import derive_endpoint_binding_hash, derive_vehicle_identity_hash
from multidetect.runtime_profile_binding_set import verify_runtime_profile_binding_set
from multidetect.runtime_profiles import sign_runtime_profile_document

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "configs/schemas/px4-mission-feedback-profile-binding.schema.json"
PROFILE_KEY = b"px4-mission-feedback-profile-join-test-key-material"
PROFILE_KEY_ID = "px4-feedback-join-key-v1"
PX4_PATH_HASH = "a" * 64
SIGNING_HASH = hashlib.sha256(b"join-signing-key").hexdigest()
ADAPTERS = {
    "fixed-wing": (PX4_FIXED_WING_ADAPTER_ID, PX4_FIXED_WING_ADAPTER_HASH),
    "multirotor": (PX4_MULTIROTOR_ADAPTER_ID, PX4_MULTIROTOR_ADAPTER_HASH),
    "vtol": (PX4_VTOL_ADAPTER_ID, PX4_VTOL_ADAPTER_HASH),
}


def _write_json(path: Path, document: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path


def _mission(vehicle_class: str) -> ExecutableMission:
    adapter_id, adapter_hash = ADAPTERS[vehicle_class]
    return ExecutableMission(
        mission_id=f"profile-join-{vehicle_class}",
        platform_plan_hash="b" * 64,
        airframe_profile_hash=hashlib.sha256(vehicle_class.encode()).hexdigest(),
        airframe_adapter_id=adapter_id,
        airframe_adapter_hash=adapter_hash,
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
        self.config = PixhawkReadOnlyConfig(endpoint="udpin:127.0.0.1:14699", expected_system_id=1)
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


def _feedback_bindings(tmp_path: Path) -> list[tuple[str, str, Path, Path, Path]]:
    missions: dict[str, Path] = {}
    for vehicle_class in ADAPTERS:
        missions[vehicle_class] = _write_json(
            tmp_path / "missions" / f"{vehicle_class}.json",
            _mission(vehicle_class).to_mapping(),
        )
    bindings: list[tuple[str, str, Path, Path, Path]] = []
    for sequence, (mode, vehicle_class) in enumerate(PX4_MISSION_FEEDBACK_REQUIRED_COMBINATIONS):
        mission = _mission(vehicle_class)
        provider = _Provider(mission)
        vehicle_hash = derive_vehicle_identity_hash(provider.heartbeat_identity)
        endpoint_hash = derive_endpoint_binding_hash(
            provider.config,
            resolved_endpoint=provider.resolved_endpoint,
        )
        profile = (
            RuntimeProfile.hil(
                vehicle_identity_hash=vehicle_hash,
                firmware_identity_hash=provider.autopilot_version_identity.identity_hash,
                airframe_profile_hash=mission.airframe_profile_hash,
                endpoint_binding_hash=endpoint_hash,
            )
            if mode == "hil"
            else RuntimeProfile.flight(
                vehicle_identity_hash=vehicle_hash,
                firmware_identity_hash=provider.autopilot_version_identity.identity_hash,
                airframe_profile_hash=mission.airframe_profile_hash,
                signing_identity_hash=SIGNING_HASH,
                endpoint_binding_hash=endpoint_hash,
            )
        )
        profile_path = _write_json(
            tmp_path / "profiles" / "feedback" / mode / f"{vehicle_class}.json",
            sign_runtime_profile_document(
                profile,
                hmac_key=PROFILE_KEY,
                key_id=PROFILE_KEY_ID,
            ),
        )
        report = observe_px4_mission_feedback(
            mission,
            provider,
            runtime_mode=mode,
            duration_s=0.0,
            clock=lambda: 10.0,
            observed_at=lambda sequence=sequence: datetime(2026, 9, 7, 12, sequence, tzinfo=UTC),
            runtime_profile_path=profile_path,
            runtime_profile_hmac_key=PROFILE_KEY,
            runtime_profile_key_id=PROFILE_KEY_ID,
            runtime_signing_identity_hash=SIGNING_HASH if mode == "real" else None,
        )
        report_path = _write_json(
            tmp_path / "feedback" / mode / f"{vehicle_class}.json",
            report,
        )
        bindings.append((mode, vehicle_class, missions[vehicle_class], profile_path, report_path))
    return bindings


def _profile_set(tmp_path: Path) -> dict[str, object]:
    identity = {
        "vehicle_identity_hash": "c" * 64,
        "firmware_identity_hash": "d" * 64,
        "airframe_profile_hash": "e" * 64,
        "endpoint_binding_hash": "f" * 64,
    }
    _write_json(
        tmp_path / "profiles" / "policy" / "replay.json",
        {"schema_version": 1, "profile": RuntimeProfile.replay().to_mapping()},
    )
    _write_json(
        tmp_path / "profiles" / "policy" / "sim.json",
        {"schema_version": 1, "profile": RuntimeProfile.sim().to_mapping()},
    )
    _write_json(
        tmp_path / "profiles" / "policy" / "hil.json",
        sign_runtime_profile_document(
            RuntimeProfile.hil(
                vehicle_identity_hash=identity["vehicle_identity_hash"],
                firmware_identity_hash=identity["firmware_identity_hash"],
                airframe_profile_hash=identity["airframe_profile_hash"],
                endpoint_binding_hash=identity["endpoint_binding_hash"],
            ),
            hmac_key=PROFILE_KEY,
            key_id=PROFILE_KEY_ID,
        ),
    )
    _write_json(
        tmp_path / "profiles" / "policy" / "real.json",
        sign_runtime_profile_document(
            RuntimeProfile.flight(
                vehicle_identity_hash=identity["vehicle_identity_hash"],
                firmware_identity_hash=identity["firmware_identity_hash"],
                airframe_profile_hash=identity["airframe_profile_hash"],
                signing_identity_hash=SIGNING_HASH,
                endpoint_binding_hash=identity["endpoint_binding_hash"],
            ),
            hmac_key=PROFILE_KEY,
            key_id=PROFILE_KEY_ID,
        ),
    )
    return verify_runtime_profile_binding_set(
        [
            {
                "runtime_mode": "replay",
                "profile_path": "profiles/policy/replay.json",
            },
            {"runtime_mode": "sim", "profile_path": "profiles/policy/sim.json"},
            {
                "runtime_mode": "hil",
                "profile_path": "profiles/policy/hil.json",
                "observed_identity": identity,
            },
            {
                "runtime_mode": "real",
                "profile_path": "profiles/policy/real.json",
                "observed_identity": {**identity, "signing_identity_hash": SIGNING_HASH},
            },
        ],
        base_directory=tmp_path,
        hmac_keys={"hil": PROFILE_KEY, "real": PROFILE_KEY},
        expected_key_ids={"hil": PROFILE_KEY_ID, "real": PROFILE_KEY_ID},
    )


def test_profile_set_is_bound_to_all_six_feedback_leaves(tmp_path: Path) -> None:
    bindings = _feedback_bindings(tmp_path)
    feedback = build_px4_mission_feedback_binding(
        bindings,
        manifest_directory=tmp_path,
        output_identity=tmp_path / "joined.json",
        release_label="0.3.0-pre.8",
        px4_mission_path_hash=PX4_PATH_HASH,
    )
    profile_set_path = _write_json(tmp_path / "profile-set.json", _profile_set(tmp_path))
    feedback_path = _write_json(tmp_path / "feedback-binding.json", feedback)
    joined = verify_px4_mission_feedback_profile_binding(
        profile_set_path,
        feedback_path,
        base_directory=tmp_path,
        expected_release_label="0.3.0-pre.8",
        expected_px4_mission_path_hash=PX4_PATH_HASH,
    )
    assert joined["schema"] == SCHEMA
    assert joined["gate_passed"] is True, joined["reasons"]
    assert all(joined["checks"].values())
    assert len(joined["leaf_bindings"]) == 6
    assert all(leaf["gate_passed"] for leaf in joined["leaf_bindings"])
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(joined)


def test_incomplete_profile_set_fails_without_erasing_feedback_failures(tmp_path: Path) -> None:
    bindings = _feedback_bindings(tmp_path)
    feedback = build_px4_mission_feedback_binding(
        bindings,
        manifest_directory=tmp_path,
        output_identity=tmp_path / "joined.json",
        release_label="0.3.0-pre.8",
        px4_mission_path_hash=PX4_PATH_HASH,
    )
    profile_set = _profile_set(tmp_path)
    profile_set["complete"] = False
    result = verify_px4_mission_feedback_profile_binding(
        profile_set,
        feedback,
        base_directory=tmp_path,
        expected_release_label="0.3.0-pre.8",
        expected_px4_mission_path_hash=PX4_PATH_HASH,
    )
    assert result["gate_passed"] is False
    assert result["checks"]["profile_set_gate_passed"] is False
    assert result["checks"]["feedback_binding_gate_passed"] is True
    assert any("profile set complete must be true" in reason for reason in result["reasons"])


def test_leaf_key_drift_fails_the_join_even_when_sources_exist(tmp_path: Path) -> None:
    bindings = _feedback_bindings(tmp_path)
    feedback = build_px4_mission_feedback_binding(
        bindings,
        manifest_directory=tmp_path,
        output_identity=tmp_path / "joined.json",
        release_label="0.3.0-pre.8",
        px4_mission_path_hash=PX4_PATH_HASH,
    )
    feedback["observations"][0]["runtime_profile_key_id"] = "wrong-key"
    result = verify_px4_mission_feedback_profile_binding(
        _profile_set(tmp_path),
        feedback,
        base_directory=tmp_path,
        expected_release_label="0.3.0-pre.8",
        expected_px4_mission_path_hash=PX4_PATH_HASH,
    )
    assert result["gate_passed"] is False
    assert result["checks"]["runtime_profile_keys_match"] is False
    assert result["checks"]["feedback_binding_gate_passed"] is False
    assert any(
        "runtime_profile_key_id does not match mode policy" in reason
        for reason in result["reasons"]
    )


def test_cli_writes_schema_valid_error_for_missing_input(tmp_path: Path) -> None:
    output = tmp_path / "error.json"
    assert (
        main(
            [
                "--profile-set",
                "missing-profile.json",
                "--feedback-binding",
                "missing-feedback.json",
                "--base-directory",
                str(tmp_path),
                "--out",
                str(output),
            ]
        )
        == 1
    )
    document = json.loads(output.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(document)
    assert document["event"] == "px4_mission_feedback_profile_binding_error"
