from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import multidetect.px4_mission_feedback_cli as feedback_cli
from multidetect.autonomy.contracts import (
    ExecutableMission,
    MissionCommand,
    MissionFrame,
    MissionItem,
)
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
from multidetect.px4_mission_feedback_cli import (
    Px4MissionFeedbackObservationError,
    provision_px4_mission_feedback_runtime_profile,
    write_px4_mission_feedback_runtime_profile,
)
from multidetect.runtime import RuntimeMode, RuntimeProfileId
from multidetect.runtime_profiles import (
    RuntimeProfileDocumentError,
    load_runtime_profile_document,
)

ROOT = Path(__file__).resolve().parents[1]
HMAC_KEY = b"profile-provision-test-key-material-at-least-32-bytes"
KEY_ID = "field-runtime-profile-test-v1"
SIGNING_KEY_HEX = "42" * 32
SIGNING_IDENTITY_HASH = hashlib.sha256(bytes.fromhex(SIGNING_KEY_HEX)).hexdigest()
_ADAPTERS = {
    "fixed-wing": (PX4_FIXED_WING_ADAPTER_ID, PX4_FIXED_WING_ADAPTER_HASH),
    "multirotor": (PX4_MULTIROTOR_ADAPTER_ID, PX4_MULTIROTOR_ADAPTER_HASH),
    "vtol": (PX4_VTOL_ADAPTER_ID, PX4_VTOL_ADAPTER_HASH),
}
_VEHICLE_TYPES = {
    "fixed-wing": 1,
    "multirotor": 2,
    "vtol": 21,
}


def _autopilot_version_identity(
    *,
    source_system_id: int = 1,
    source_component_id: int = 1,
) -> PixhawkAutopilotVersionIdentity:
    return PixhawkAutopilotVersionIdentity(
        source_system_id=source_system_id,
        source_component_id=source_component_id,
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
        device_uid_source="uid2",
        device_uid_hash=hashlib.sha256(b"synthetic-profile-device-uid2").hexdigest(),
    )


def _mission(vehicle_class: str) -> ExecutableMission:
    adapter_id, adapter_hash = _ADAPTERS[vehicle_class]
    return ExecutableMission(
        mission_id=f"profile-provision-{vehicle_class}",
        platform_plan_hash="a" * 64,
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


class _ProfileProvider:
    def __init__(
        self,
        config: PixhawkReadOnlyConfig,
        *,
        vehicle_type_id: int = 1,
        source_component_id: int = 1,
        autopilot_id: int = 12,
        system_status_id: int = 4,
        resolved_endpoint: str | None = None,
        read_only: bool = True,
        link_healthy: bool = True,
        messages_received: int = 1,
        messages_transmitted: int = 0,
        fail_receive: bool = False,
        provide_autopilot_version: bool = True,
        autopilot_version_system_id: int | None = None,
        autopilot_version_component_id: int | None = None,
        autopilot_version_observed_at_s: float | None = None,
        refresh_autopilot_version_at_snapshot: bool = True,
    ) -> None:
        self.config = config
        self.resolved_endpoint = (
            config.endpoint if resolved_endpoint is None else resolved_endpoint
        )
        self.heartbeat_identity = PixhawkHeartbeatIdentity(
            source_system_id=config.expected_system_id,
            source_component_id=source_component_id,
            autopilot_id=autopilot_id,
            vehicle_type_id=vehicle_type_id,
            system_status_id=system_status_id,
            mavlink_version=3,
        )
        self.is_read_only = read_only
        self.messages_received = messages_received
        self.messages_transmitted = messages_transmitted
        self.autopilot_version_identity = (
            _autopilot_version_identity(
                source_system_id=(
                    config.expected_system_id
                    if autopilot_version_system_id is None
                    else autopilot_version_system_id
                ),
                source_component_id=(
                    source_component_id
                    if autopilot_version_component_id is None
                    else autopilot_version_component_id
                ),
            )
            if provide_autopilot_version
            else None
        )
        self.autopilot_version_observed_at_s = autopilot_version_observed_at_s
        self._refresh_autopilot_version_at_snapshot = (
            refresh_autopilot_version_at_snapshot
        )
        self._link_healthy = link_healthy
        self._fail_receive = fail_receive
        self.closed = False

    def snapshot(self, *, now_s: float) -> object:
        if self._fail_receive:
            raise RuntimeError("synthetic heartbeat receive failure")
        if (
            self._refresh_autopilot_version_at_snapshot
            and self.autopilot_version_identity is not None
        ):
            self.autopilot_version_observed_at_s = now_s
        return object()

    def transport_link_healthy(self, *, now_s: float) -> bool:
        return self._link_healthy

    def close(self) -> None:
        self.closed = True


def _provider(vehicle_class: str, **kwargs: object) -> _ProfileProvider:
    options = {"vehicle_type_id": _VEHICLE_TYPES[vehicle_class], **kwargs}
    return _ProfileProvider(
        PixhawkReadOnlyConfig(
            endpoint="udpin:127.0.0.1:14599",
            expected_system_id=1,
            expected_autopilot_id=12,
            require_operational_state=True,
            request_ranging_streams=False,
        ),
        **options,
    )


@pytest.mark.parametrize("vehicle_class", tuple(_ADAPTERS))
@pytest.mark.parametrize("runtime_mode", ("hil", "real"))
def test_profile_provision_binds_fresh_px4_endpoint_mission_and_vehicle(
    tmp_path: Path,
    vehicle_class: str,
    runtime_mode: str,
) -> None:
    mission = _mission(vehicle_class)
    provision = provision_px4_mission_feedback_runtime_profile(
        mission,
        _provider(vehicle_class),
        runtime_mode=runtime_mode,
        hmac_key=HMAC_KEY,
        key_id=KEY_ID,
        signing_identity_hash=(
            SIGNING_IDENTITY_HASH if runtime_mode == "real" else None
        ),
        duration_s=0.0,
        clock=lambda: 10.0,
    )

    assert provision.vehicle_class == vehicle_class
    assert provision.profile.profile_id is (
        RuntimeProfileId.HIL if runtime_mode == "hil" else RuntimeProfileId.FLIGHT
    )
    assert provision.profile.airframe_profile_hash == mission.airframe_profile_hash
    assert provision.profile.vehicle_identity_hash == (
        provision.runtime_identity.vehicle_identity_hash
    )
    assert provision.profile.firmware_identity_hash == (
        provision.autopilot_version_identity.identity_hash
    )
    assert provision.runtime_identity.firmware_identity_hash == (
        provision.autopilot_version_identity.identity_hash
    )
    assert provision.autopilot_version_identity.to_document()[
        "raw_device_uid_serialized"
    ] is False
    assert provision.profile.endpoint_binding_hash == (
        provision.runtime_identity.endpoint_binding_hash
    )
    assert provision.messages_transmitted == 0

    output = write_px4_mission_feedback_runtime_profile(
        tmp_path / f"{runtime_mode}-{vehicle_class}.json",
        provision,
        hmac_key=HMAC_KEY,
    )
    reopened = load_runtime_profile_document(
        output,
        expected_mode=RuntimeMode(runtime_mode),
        observed_identity=provision.runtime_identity,
        hmac_key=HMAC_KEY,
        expected_key_id=KEY_ID,
        require_signature=True,
        allow_flight_control_output=False,
    )
    assert reopened.profile == provision.profile
    assert reopened.profile_valid is True
    assert reopened.output_locked is True
    assert reopened.flight_control_enabled is False
    serialized = output.read_text(encoding="utf-8")
    assert HMAC_KEY.decode() not in serialized
    assert SIGNING_KEY_HEX not in serialized


@pytest.mark.parametrize(
    ("provider_overrides", "reason"),
    (
        ({"vehicle_type_id": 2}, "vehicle_type_does_not_match_mission"),
        ({"source_component_id": 42}, "component_id_mismatch"),
        ({"autopilot_id": 3}, "autopilot_is_not_px4"),
        ({"system_status_id": 5}, "system_status_not_operational"),
        ({"link_healthy": False}, "heartbeat_not_fresh"),
        ({"read_only": False}, "provider_not_read_only"),
        ({"messages_received": 0}, "heartbeat_not_received"),
        ({"resolved_endpoint": "auto"}, "endpoint_not_resolved"),
        ({"messages_transmitted": 1}, "mavlink_messages_transmitted"),
        ({"fail_receive": True}, "mavlink_receive_errors"),
    ),
)
def test_profile_provision_rejects_unqualified_or_non_read_only_identity(
    provider_overrides: dict[str, object],
    reason: str,
) -> None:
    with pytest.raises(Px4MissionFeedbackObservationError, match=reason):
        provision_px4_mission_feedback_runtime_profile(
            _mission("fixed-wing"),
            _provider("fixed-wing", **provider_overrides),
            runtime_mode="hil",
            hmac_key=HMAC_KEY,
            key_id=KEY_ID,
            duration_s=0.0,
            clock=lambda: 10.0,
        )


@pytest.mark.parametrize(
    ("provider_overrides", "reason"),
    (
        (
            {"provide_autopilot_version": False},
            "autopilot_version_identity_missing",
        ),
        (
            {"autopilot_version_component_id": 42},
            "autopilot_version_source_does_not_match_heartbeat",
        ),
        (
            {
                "autopilot_version_observed_at_s": 9.0,
                "refresh_autopilot_version_at_snapshot": False,
            },
            "autopilot_version_not_observed_during_probe",
        ),
        (
            {
                "autopilot_version_observed_at_s": 11.0,
                "refresh_autopilot_version_at_snapshot": False,
            },
            "autopilot_version_not_observed_during_probe",
        ),
    ),
)
def test_profile_provision_requires_current_source_firmware_identity(
    provider_overrides: dict[str, object],
    reason: str,
) -> None:
    with pytest.raises(Px4MissionFeedbackObservationError, match=reason):
        provision_px4_mission_feedback_runtime_profile(
            _mission("fixed-wing"),
            _provider("fixed-wing", **provider_overrides),
            runtime_mode="hil",
            hmac_key=HMAC_KEY,
            key_id=KEY_ID,
            duration_s=0.0,
            clock=lambda: 10.0,
        )


def test_profile_provision_rejects_mode_signing_and_adapter_mismatch() -> None:
    mission = _mission("fixed-wing")
    provider = _provider("fixed-wing")
    with pytest.raises(Px4MissionFeedbackObservationError, match="cannot carry"):
        provision_px4_mission_feedback_runtime_profile(
            mission,
            provider,
            runtime_mode="hil",
            hmac_key=HMAC_KEY,
            key_id=KEY_ID,
            signing_identity_hash=SIGNING_IDENTITY_HASH,
            duration_s=0.0,
            clock=lambda: 10.0,
        )
    with pytest.raises(Px4MissionFeedbackObservationError, match="requires a signing"):
        provision_px4_mission_feedback_runtime_profile(
            mission,
            provider,
            runtime_mode="real",
            hmac_key=HMAC_KEY,
            key_id=KEY_ID,
            duration_s=0.0,
            clock=lambda: 10.0,
        )
    with pytest.raises(Px4MissionFeedbackObservationError, match="at least 32 bytes"):
        provision_px4_mission_feedback_runtime_profile(
            mission,
            provider,
            runtime_mode="hil",
            hmac_key=b"short",
            key_id=KEY_ID,
            duration_s=0.0,
            clock=lambda: 10.0,
        )

    wrong_adapter = ExecutableMission(
        mission_id=mission.mission_id,
        platform_plan_hash=mission.platform_plan_hash,
        airframe_profile_hash=mission.airframe_profile_hash,
        airframe_adapter_id="untrusted-adapter",
        airframe_adapter_hash=mission.airframe_adapter_hash,
        items=mission.items,
    )
    with pytest.raises(Px4MissionFeedbackObservationError, match="current fixed-wing"):
        provision_px4_mission_feedback_runtime_profile(
            wrong_adapter,
            provider,
            runtime_mode="hil",
            hmac_key=HMAC_KEY,
            key_id=KEY_ID,
            duration_s=0.0,
            clock=lambda: 10.0,
        )


def test_profile_writer_verifies_key_and_refuses_implicit_overwrite(tmp_path: Path) -> None:
    provision = provision_px4_mission_feedback_runtime_profile(
        _mission("fixed-wing"),
        _provider("fixed-wing"),
        runtime_mode="hil",
        hmac_key=HMAC_KEY,
        key_id=KEY_ID,
        duration_s=0.0,
        clock=lambda: 10.0,
    )
    output = tmp_path / "runtime-profile.json"
    with pytest.raises(RuntimeProfileDocumentError, match="signature mismatch"):
        write_px4_mission_feedback_runtime_profile(
            output,
            provision,
            hmac_key=b"different-profile-key-material-at-least-32-bytes",
        )
    assert not output.exists()

    write_px4_mission_feedback_runtime_profile(output, provision, hmac_key=HMAC_KEY)
    original = output.read_bytes()
    with pytest.raises(Px4MissionFeedbackObservationError, match="already exists"):
        write_px4_mission_feedback_runtime_profile(output, provision, hmac_key=HMAC_KEY)
    assert output.read_bytes() == original


def test_provision_cli_reads_secrets_from_environment_and_dispatches_from_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mission = _mission("fixed-wing")
    mission_path = tmp_path / "mission.json"
    mission_path.write_text(json.dumps(mission.to_mapping()), encoding="utf-8")
    output = tmp_path / "runtime-profile-real.json"
    providers: list[_ProfileProvider] = []

    class _CliProvider(_ProfileProvider):
        def __init__(self, config: PixhawkReadOnlyConfig) -> None:
            super().__init__(config, vehicle_type_id=1)
            providers.append(self)

    monkeypatch.setattr(feedback_cli, "PixhawkReadOnlyTelemetryProvider", _CliProvider)
    monkeypatch.setenv("MULTIDETECT_TEST_PROFILE_HMAC", HMAC_KEY.decode())
    monkeypatch.setenv("MULTIDETECT_TEST_MAVLINK_SIGNING", SIGNING_KEY_HEX)
    arguments = [
        "--mission",
        str(mission_path),
        "--endpoint",
        "udpin:127.0.0.1:14599",
        "--runtime-mode",
        "real",
        "--runtime-profile-hmac-key-env",
        "MULTIDETECT_TEST_PROFILE_HMAC",
        "--runtime-profile-key-id",
        KEY_ID,
        "--runtime-signing-key-hex-env",
        "MULTIDETECT_TEST_MAVLINK_SIGNING",
        "--probe-duration-seconds",
        "0",
        "--out",
        str(output),
    ]

    assert feedback_cli.main(["provision", *arguments]) == 0
    assert output.is_file()
    summary = json.loads(capsys.readouterr().out)
    assert summary["runtime_mode"] == "real"
    assert summary["vehicle_class"] == "fixed-wing"
    assert summary["output_locked"] is True
    assert summary["transport"]["messages_transmitted"] == 0
    assert all(provider.closed for provider in providers)
    assert HMAC_KEY.decode() not in json.dumps(summary)
    assert SIGNING_KEY_HEX not in json.dumps(summary)

    assert feedback_cli.provision_main(arguments) == 2
    assert output.is_file()
    capsys.readouterr()
    assert feedback_cli.provision_main([*arguments, "--overwrite"]) == 0


def test_packaging_exposes_profile_provision_command() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert (
        'multidetect-px4-runtime-profile-provision = '
        '"multidetect.px4_mission_feedback_cli:provision_main"'
    ) in pyproject


def test_documentation_explains_profile_bootstrap_and_evidence_boundary() -> None:
    documents = {
        "README.md": (
            "v44 现场 RuntimeProfile 自动签发",
            "增加 `provision` 子命令",
            "multidetect-px4-runtime-profile-provision",
            "messages_transmitted=0",
            "profile 只是后续 v2 observer 的能力输入",
        ),
        "CHANGELOG.md": (
            "v44 PX4 RuntimeProfile 现场自动签发",
            "默认不覆盖既有 profile",
        ),
        "docs/release-gate.md": (
            "v44 现场 profile provision 门",
            "只有 `profile_valid=true`、`output_locked=true`",
        ),
        "docs/field-bringup.md": (
            "HIL/REAL Mission feedback profile 现场签发",
            "profile 签发本身不是 HIL/REAL 通过证据",
        ),
        "docs/autonomy-architecture.md": (
            "从实际 heartbeat provision 签名 profile",
            "不产生验收 claim",
        ),
        "docs/repository-architecture.md": (
            "v44 RuntimeProfile 现场 provisioning",
            "不启动 Mission",
        ),
    }
    for relative_path, tokens in documents.items():
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        for token in tokens:
            assert token in text, f"{relative_path} is missing {token!r}"
