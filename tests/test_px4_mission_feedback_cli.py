from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

import multidetect.px4_mission_feedback_cli as feedback_cli
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
from multidetect.px4_mission_feedback import (
    Px4MissionFeedbackConfig,
    Px4MissionFeedbackTracker,
)
from multidetect.px4_mission_feedback_cli import (
    Px4MissionFeedbackObservationError,
    load_px4_mission_feedback_observation,
    observe_px4_mission_feedback,
    verify_px4_mission_feedback_observation,
    write_px4_mission_feedback_observation,
)
from multidetect.runtime import RuntimeProfile
from multidetect.runtime_launcher import (
    derive_endpoint_binding_hash,
    derive_vehicle_identity_hash,
)
from multidetect.runtime_profiles import sign_runtime_profile_document

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_KEY = b"px4-feedback-runtime-profile-test-key-material"
RUNTIME_KEY_ID = "px4-feedback-test-key"
SIGNING_IDENTITY_HASH = hashlib.sha256(b"mavlink-signing-test-key").hexdigest()


def _autopilot_version_identity() -> PixhawkAutopilotVersionIdentity:
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


def _mission() -> ExecutableMission:
    return ExecutableMission(
        mission_id="field-feedback-test",
        platform_plan_hash="a" * 64,
        airframe_profile_hash="b" * 64,
        airframe_adapter_id="test-airframe-adapter",
        airframe_adapter_hash="c" * 64,
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


class _FakeProvider:
    def __init__(
        self,
        tracker: Px4MissionFeedbackTracker,
        *,
        emit_feedback: bool = True,
        fail_receive: bool = False,
        read_only: bool = True,
        messages_transmitted: int = 0,
    ) -> None:
        self.config = PixhawkReadOnlyConfig(
            endpoint="udpin:127.0.0.1:14599",
            expected_system_id=1,
        )
        self.resolved_endpoint = self.config.endpoint
        self.heartbeat_identity = PixhawkHeartbeatIdentity(
            source_system_id=1,
            source_component_id=1,
            autopilot_id=12,
            vehicle_type_id=1,
            system_status_id=4,
            mavlink_version=3,
        )
        self.autopilot_version_identity = _autopilot_version_identity()
        self.autopilot_version_observed_at_s = 10.0
        self.is_read_only = read_only
        self.messages_received = 0
        self.messages_transmitted = messages_transmitted
        self.message_type_counts: dict[str, int] = {}
        self._tracker = tracker
        self._emit_feedback = emit_feedback
        self._fail_receive = fail_receive
        self._emitted = False

    def snapshot(self, *, now_s: float) -> object:
        if self._fail_receive:
            raise RuntimeError("synthetic receive failure")
        if self._emit_feedback and not self._emitted:
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


def _provider(
    mission: ExecutableMission,
    **kwargs,
) -> _FakeProvider:
    tracker = Px4MissionFeedbackTracker(
        mission,
        config=Px4MissionFeedbackConfig(
            expected_system_id=1,
            expected_component_id=1,
        ),
    )
    return _FakeProvider(tracker, **kwargs)


def _observe(provider: _FakeProvider, mission: ExecutableMission) -> dict[str, object]:
    return observe_px4_mission_feedback(
        mission,
        provider,
        runtime_mode="hil",
        duration_s=0.0,
        clock=lambda: 10.0,
        observed_at=lambda: datetime(2026, 9, 4, 12, 0, tzinfo=UTC),
    )


def _runtime_profile(
    tmp_path: Path,
    mission: ExecutableMission,
    provider: _FakeProvider,
    *,
    mode: str,
    include_firmware: bool = True,
) -> Path:
    vehicle_identity_hash = derive_vehicle_identity_hash(provider.heartbeat_identity)
    endpoint_binding_hash = derive_endpoint_binding_hash(
        provider.config,
        resolved_endpoint=provider.resolved_endpoint,
    )
    if mode == "hil":
        profile = RuntimeProfile.hil(
            vehicle_identity_hash=vehicle_identity_hash,
            firmware_identity_hash=(
                provider.autopilot_version_identity.identity_hash if include_firmware else None
            ),
            airframe_profile_hash=mission.airframe_profile_hash,
            endpoint_binding_hash=endpoint_binding_hash,
        )
    else:
        profile = RuntimeProfile.flight(
            vehicle_identity_hash=vehicle_identity_hash,
            firmware_identity_hash=(
                provider.autopilot_version_identity.identity_hash if include_firmware else None
            ),
            airframe_profile_hash=mission.airframe_profile_hash,
            signing_identity_hash=SIGNING_IDENTITY_HASH,
            endpoint_binding_hash=endpoint_binding_hash,
        )
    path = tmp_path / f"runtime-profile-{mode}.json"
    path.write_text(
        json.dumps(
            sign_runtime_profile_document(
                profile,
                hmac_key=RUNTIME_KEY,
                key_id=RUNTIME_KEY_ID,
            )
        ),
        encoding="utf-8",
    )
    return path


def _observe_bound(
    tmp_path: Path,
    provider: _FakeProvider,
    mission: ExecutableMission,
    *,
    mode: str = "hil",
) -> tuple[dict[str, object], Path]:
    profile_path = _runtime_profile(tmp_path, mission, provider, mode=mode)
    report = observe_px4_mission_feedback(
        mission,
        provider,
        runtime_mode=mode,
        duration_s=0.0,
        clock=lambda: 10.0,
        observed_at=lambda: datetime(2026, 9, 4, 12, 0, tzinfo=UTC),
        runtime_profile_path=profile_path,
        runtime_profile_hmac_key=RUNTIME_KEY,
        runtime_profile_key_id=RUNTIME_KEY_ID,
        runtime_signing_identity_hash=(SIGNING_IDENTITY_HASH if mode == "real" else None),
    )
    return report, profile_path


def test_observer_writes_and_reloads_hash_bound_receive_only_report(tmp_path: Path) -> None:
    mission = _mission()
    report = _observe(_provider(mission), mission)

    assert report["feedback_gate_passed"] is True
    assert report["reasons"] == ()
    assert report["transport"]["read_only"] is True
    assert report["transport"]["messages_transmitted"] == 0
    assert report["feedback"]["reason"] == "terminal_mission_item_reached"
    assert report["claims"] == {
        "source_kind": "live_mavlink_receive_only",
        "release_eligible": False,
        "hardware_acceptance": False,
        "hil_acceptance": False,
        "real_acceptance": False,
        "operator_visual_acceptance": False,
    }

    output = write_px4_mission_feedback_observation(tmp_path / "feedback.json", report)
    reloaded = load_px4_mission_feedback_observation(output, expected_mission=mission)
    assert reloaded["evidence_sha256"] == report["evidence_sha256"]
    assert reloaded["schema_version"] == 1


@pytest.mark.parametrize("mode", ("hil", "real"))
def test_v2_observer_binds_signed_runtime_vehicle_endpoint_and_airframe(
    tmp_path: Path,
    mode: str,
) -> None:
    mission = _mission()
    provider = _provider(mission)
    report, profile_path = _observe_bound(
        tmp_path,
        provider,
        mission,
        mode=mode,
    )

    assert report["schema_version"] == 2
    assert report["feedback_gate_passed"] is True
    assert report["runtime_binding"]["gate_passed"] is True
    assert report["runtime_binding"]["profile"]["profile_id"] == (
        "hil" if mode == "hil" else "flight"
    )
    assert report["runtime_binding"]["flight_control_enabled"] is False
    assert report["runtime_binding"]["output_locked"] is True
    assert (
        report["runtime_binding"]["observed_identity"]["firmware_identity_hash"]
        == provider.autopilot_version_identity.identity_hash
    )
    assert RUNTIME_KEY.decode() not in json.dumps(report)

    output = write_px4_mission_feedback_observation(
        tmp_path / f"feedback-{mode}.json",
        report,
    )
    reopened = load_px4_mission_feedback_observation(
        output,
        expected_mission=mission,
        runtime_profile_path=profile_path,
    )
    assert reopened["evidence_sha256"] == report["evidence_sha256"]


def test_v2_hil_observer_rejects_real_signing_identity(tmp_path: Path) -> None:
    mission = _mission()
    provider = _provider(mission)
    profile_path = _runtime_profile(tmp_path, mission, provider, mode="hil")

    report = observe_px4_mission_feedback(
        mission,
        provider,
        runtime_mode="hil",
        duration_s=0.0,
        clock=lambda: 10.0,
        observed_at=lambda: datetime(2026, 9, 4, 12, 0, tzinfo=UTC),
        runtime_profile_path=profile_path,
        runtime_profile_hmac_key=RUNTIME_KEY,
        runtime_profile_key_id=RUNTIME_KEY_ID,
        runtime_signing_identity_hash=SIGNING_IDENTITY_HASH,
    )

    assert report["feedback_gate_passed"] is False
    assert report["runtime_binding"]["gate_passed"] is False
    assert report["runtime_binding"]["reasons"] == ("hil_runtime_must_not_bind_signing_identity",)
    assert report["reasons"] == ("runtime_binding:hil_runtime_must_not_bind_signing_identity",)


def test_v2_observer_requires_profile_and_observed_firmware_identity(
    tmp_path: Path,
) -> None:
    mission = _mission()

    legacy_provider = _provider(mission)
    legacy_profile = _runtime_profile(
        tmp_path,
        mission,
        legacy_provider,
        mode="hil",
        include_firmware=False,
    )
    legacy_report = observe_px4_mission_feedback(
        mission,
        legacy_provider,
        runtime_mode="hil",
        duration_s=0.0,
        clock=lambda: 10.0,
        observed_at=lambda: datetime(2026, 9, 4, 12, 0, tzinfo=UTC),
        runtime_profile_path=legacy_profile,
        runtime_profile_hmac_key=RUNTIME_KEY,
        runtime_profile_key_id=RUNTIME_KEY_ID,
    )
    assert legacy_report["feedback_gate_passed"] is False
    assert (
        "runtime_profile_firmware_identity_missing" in legacy_report["runtime_binding"]["reasons"]
    )

    missing_provider = _provider(mission)
    missing_profile = _runtime_profile(
        tmp_path,
        mission,
        missing_provider,
        mode="real",
    )
    missing_provider.autopilot_version_identity = None
    missing_report = observe_px4_mission_feedback(
        mission,
        missing_provider,
        runtime_mode="real",
        duration_s=0.0,
        clock=lambda: 10.0,
        observed_at=lambda: datetime(2026, 9, 4, 12, 0, tzinfo=UTC),
        runtime_profile_path=missing_profile,
        runtime_profile_hmac_key=RUNTIME_KEY,
        runtime_profile_key_id=RUNTIME_KEY_ID,
        runtime_signing_identity_hash=SIGNING_IDENTITY_HASH,
    )
    assert missing_report["feedback_gate_passed"] is False
    assert "observed_firmware_identity_missing" in missing_report["runtime_binding"]["reasons"]

    mismatch_provider = _provider(mission)
    mismatch_profile = _runtime_profile(
        tmp_path,
        mission,
        mismatch_provider,
        mode="hil",
    )
    mismatch_provider.autopilot_version_identity = replace(
        mismatch_provider.autopilot_version_identity,
        product_id=43,
    )
    mismatch_report = observe_px4_mission_feedback(
        mission,
        mismatch_provider,
        runtime_mode="hil",
        duration_s=0.0,
        clock=lambda: 10.0,
        observed_at=lambda: datetime(2026, 9, 4, 12, 0, tzinfo=UTC),
        runtime_profile_path=mismatch_profile,
        runtime_profile_hmac_key=RUNTIME_KEY,
        runtime_profile_key_id=RUNTIME_KEY_ID,
    )
    assert mismatch_report["feedback_gate_passed"] is False
    assert (
        "runtime_profile_firmware_identity_mismatch"
        in mismatch_report["runtime_binding"]["reasons"]
    )


def test_v2_observer_rejects_endpoint_heartbeat_and_profile_file_tampering(
    tmp_path: Path,
) -> None:
    mission = _mission()
    report, profile_path = _observe_bound(tmp_path, _provider(mission), mission)

    endpoint_forgery = copy.deepcopy(report)
    endpoint_forgery["transport"]["resolved_endpoint"] = "udpin:127.0.0.1:14600"
    endpoint_forgery["evidence_sha256"] = feedback_cli.canonical_sha256(
        {key: value for key, value in endpoint_forgery.items() if key != "evidence_sha256"}
    )
    with pytest.raises(Px4MissionFeedbackObservationError, match="endpoint identity"):
        verify_px4_mission_feedback_observation(endpoint_forgery)

    heartbeat_forgery = copy.deepcopy(report)
    heartbeat_forgery["runtime_binding"]["heartbeat_identity"]["vehicle_type_id"] = 2
    runtime_without_hash = {
        key: value
        for key, value in heartbeat_forgery["runtime_binding"].items()
        if key != "binding_sha256"
    }
    heartbeat_forgery["runtime_binding"]["binding_sha256"] = feedback_cli.canonical_sha256(
        runtime_without_hash
    )
    heartbeat_forgery["evidence_sha256"] = feedback_cli.canonical_sha256(
        {key: value for key, value in heartbeat_forgery.items() if key != "evidence_sha256"}
    )
    with pytest.raises(Px4MissionFeedbackObservationError, match="vehicle identity"):
        verify_px4_mission_feedback_observation(heartbeat_forgery)

    profile_path.write_bytes(profile_path.read_bytes() + b"\n")
    with pytest.raises(Px4MissionFeedbackObservationError, match="file SHA-256"):
        verify_px4_mission_feedback_observation(
            report,
            expected_mission=mission,
            runtime_profile_path=profile_path,
        )


def test_observer_reports_missing_feedback_and_receive_errors_fail_closed() -> None:
    mission = _mission()
    missing = _observe(_provider(mission, emit_feedback=False), mission)
    failed = _observe(_provider(mission, emit_feedback=False, fail_receive=True), mission)

    assert missing["feedback_gate_passed"] is False
    assert missing["reasons"] == ("feedback:mission_feedback_unavailable",)
    assert failed["feedback_gate_passed"] is False
    assert failed["reasons"] == (
        "mavlink_receive_errors",
        "feedback:mission_feedback_unavailable",
    )
    assert failed["sampling"]["receive_error_count"] == 1
    assert failed["sampling"]["receive_error_samples"][0]["error_type"] == "RuntimeError"


def test_observer_rejects_forged_gate_feedback_and_evidence_hash() -> None:
    mission = _mission()
    report = _observe(_provider(mission), mission)

    forged_gate = copy.deepcopy(report)
    forged_gate["feedback_gate_passed"] = False
    with pytest.raises(Px4MissionFeedbackObservationError, match="recomputed gate"):
        verify_px4_mission_feedback_observation(forged_gate)

    forged_feedback = copy.deepcopy(report)
    forged_feedback["feedback_sha256"] = "c" * 64
    with pytest.raises(Px4MissionFeedbackObservationError, match="feedback_sha256"):
        verify_px4_mission_feedback_observation(forged_feedback)

    forged_hash = copy.deepcopy(report)
    forged_hash["evidence_sha256"] = "d" * 64
    with pytest.raises(Px4MissionFeedbackObservationError, match="evidence_sha256"):
        verify_px4_mission_feedback_observation(forged_hash)


def test_observer_cannot_claim_hardware_or_transmit_mavlink() -> None:
    mission = _mission()
    report = _observe(
        _provider(mission, read_only=False, messages_transmitted=1),
        mission,
    )
    assert report["feedback_gate_passed"] is False
    assert report["reasons"] == (
        "provider_not_read_only",
        "mavlink_messages_transmitted",
    )

    forged = copy.deepcopy(report)
    forged["claims"]["hil_acceptance"] = True
    with pytest.raises(Px4MissionFeedbackObservationError, match="must remain false"):
        verify_px4_mission_feedback_observation(forged)


def test_observation_schema_is_valid() -> None:
    schema = json.loads(
        (ROOT / "configs/schemas/px4-mission-feedback-observation.schema.json").read_text(
            encoding="utf-8"
        )
    )
    feedback_schema = json.loads(
        (ROOT / "configs/schemas/px4-mission-feedback.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    registry = Registry().with_resource(
        feedback_schema["$id"],
        Resource.from_contents(feedback_schema),
    )
    report = json.loads(json.dumps(_observe(_provider(_mission()), _mission())))
    Draft202012Validator(
        schema,
        registry=registry,
        format_checker=FormatChecker(),
    ).validate(report)


def test_v2_observation_schema_validates_runtime_binding(tmp_path: Path) -> None:
    schema = json.loads(
        (ROOT / "configs/schemas/px4-mission-feedback-observation-v2.schema.json").read_text(
            encoding="utf-8"
        )
    )
    feedback_schema = json.loads(
        (ROOT / "configs/schemas/px4-mission-feedback.schema.json").read_text(encoding="utf-8")
    )
    runtime_profile_schema = json.loads(
        (ROOT / "configs/schemas/runtime-profile.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    registry = Registry().with_resources(
        [
            (
                feedback_schema["$id"],
                Resource.from_contents(feedback_schema),
            ),
            (
                runtime_profile_schema["$id"],
                Resource.from_contents(runtime_profile_schema),
            ),
        ]
    )
    mission = _mission()
    report, _ = _observe_bound(tmp_path, _provider(mission), mission)
    Draft202012Validator(
        schema,
        registry=registry,
        format_checker=FormatChecker(),
    ).validate(json.loads(json.dumps(report)))


def test_cli_capture_and_verify_use_the_same_mission_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mission = _mission()
    mission_path = tmp_path / "mission.json"
    mission_path.write_text(json.dumps(mission.to_mapping()), encoding="utf-8")
    output = tmp_path / "observation.json"

    class _CliProvider(_FakeProvider):
        def __init__(self, config, *, mission_feedback_tracker=None) -> None:
            assert mission_feedback_tracker is not None
            super().__init__(mission_feedback_tracker)
            self.config = config

        def close(self) -> None:
            return None

    monkeypatch.setattr(feedback_cli, "PixhawkReadOnlyTelemetryProvider", _CliProvider)
    monkeypatch.setattr(feedback_cli.time, "monotonic", lambda: 10.0)
    profile_path = _runtime_profile(
        tmp_path,
        mission,
        _provider(mission),
        mode="hil",
    )
    monkeypatch.setenv("MULTIDETECT_TEST_RUNTIME_PROFILE_KEY", RUNTIME_KEY.decode())

    assert (
        feedback_cli.main(
            [
                "--mission",
                str(mission_path),
                "--endpoint",
                "udpin:127.0.0.1:14599",
                "--runtime-mode",
                "hil",
                "--runtime-profile",
                str(profile_path),
                "--runtime-profile-hmac-key-env",
                "MULTIDETECT_TEST_RUNTIME_PROFILE_KEY",
                "--runtime-profile-key-id",
                RUNTIME_KEY_ID,
                "--duration-seconds",
                "0",
                "--out",
                str(output),
            ]
        )
        == 0
    )
    verify_arguments = [
        str(output),
        "--mission",
        str(mission_path),
        "--runtime-profile",
        str(profile_path),
    ]
    assert feedback_cli.verify_main(verify_arguments) == 0
    assert feedback_cli.main(["verify", *verify_arguments]) == 0


def test_cli_report_only_preserves_unqualified_feedback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mission = _mission()
    mission_path = tmp_path / "mission.json"
    mission_path.write_text(json.dumps(mission.to_mapping()), encoding="utf-8")
    output = tmp_path / "unqualified.json"

    class _NoFeedbackProvider(_FakeProvider):
        def __init__(self, config, *, mission_feedback_tracker=None) -> None:
            assert mission_feedback_tracker is not None
            super().__init__(mission_feedback_tracker, emit_feedback=False)
            self.config = config

        def close(self) -> None:
            return None

    monkeypatch.setattr(feedback_cli, "PixhawkReadOnlyTelemetryProvider", _NoFeedbackProvider)
    monkeypatch.setattr(feedback_cli.time, "monotonic", lambda: 10.0)
    profile_path = _runtime_profile(
        tmp_path,
        mission,
        _provider(mission),
        mode="hil",
    )
    monkeypatch.setenv("MULTIDETECT_TEST_RUNTIME_PROFILE_KEY", RUNTIME_KEY.decode())

    common = [
        "--mission",
        str(mission_path),
        "--endpoint",
        "udpin:127.0.0.1:14599",
        "--runtime-mode",
        "hil",
        "--runtime-profile",
        str(profile_path),
        "--runtime-profile-hmac-key-env",
        "MULTIDETECT_TEST_RUNTIME_PROFILE_KEY",
        "--runtime-profile-key-id",
        RUNTIME_KEY_ID,
        "--duration-seconds",
        "0",
        "--out",
        str(output),
    ]
    assert feedback_cli.main(common) == 1
    assert feedback_cli.main([*common, "--report-only"]) == 0
    assert feedback_cli.verify_main([str(output)]) == 1
    assert feedback_cli.verify_main([str(output), "--allow-unqualified"]) == 0
