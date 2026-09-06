from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import multidetect.runtime_launcher as launcher_module
from multidetect.clock import ManualClock
from multidetect.fixed_wing_aim_control import PixhawkFlightControlConfig
from multidetect.pixhawk import (
    PixhawkAutopilotVersionIdentity,
    PixhawkHeartbeatIdentity,
    PixhawkReadOnlyConfig,
)
from multidetect.runtime import RuntimeMode, RuntimeProfile
from multidetect.runtime_launcher import (
    ProfileBoundFlightControlProvider,
    ProfileBoundRuntimeConfig,
    ProfileBoundRuntimeLauncher,
    build_pixhawk_runtime_identity,
    derive_endpoint_binding_hash,
    derive_vehicle_identity_hash,
)
from multidetect.runtime_profiles import sign_runtime_profile_document

KEY = b"runtime-launcher-test-key-material-32-bytes-minimum"
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


class _Message:
    def __init__(
        self,
        name: str,
        *,
        source_system_id: int = 1,
        source_component_id: int = 1,
        **values: object,
    ) -> None:
        self._name = name
        self._source_system_id = source_system_id
        self._source_component_id = source_component_id
        self.__dict__.update(values)

    def get_type(self) -> str:
        return self._name

    def get_srcSystem(self) -> int:
        return self._source_system_id

    def get_srcComponent(self) -> int:
        return self._source_component_id


class _QueuedConnection:
    flightmode = "AUTO"

    def __init__(self, *messages: _Message) -> None:
        self.messages = list(messages)
        self.command_calls: list[tuple[object, ...]] = []
        self.mode_calls: list[tuple[object, ...]] = []
        self.attitude_calls: list[tuple[object, ...]] = []
        self.mav = SimpleNamespace(
            command_long_send=lambda *values: self.command_calls.append(values),
            set_mode_send=lambda *values: self.mode_calls.append(values),
            set_attitude_target_send=lambda *values: self.attitude_calls.append(values),
        )
        self.closed = False

    def recv_match(self, *, blocking: bool):
        assert blocking is False
        return self.messages.pop(0) if self.messages else None

    def mode_mapping(self) -> dict[str, int]:
        return {"OFFBOARD": 6, "AUTO": 4}

    def close(self) -> None:
        self.closed = True


def _heartbeat_message() -> _Message:
    return _Message(
        "HEARTBEAT",
        autopilot=12,
        type=1,
        system_status=4,
        mavlink_version=3,
        base_mode=0,
    )


def _autopilot_version_message() -> _Message:
    identity = _autopilot_version_identity()
    return _Message(
        "AUTOPILOT_VERSION",
        capabilities=identity.capabilities,
        flight_sw_version=identity.flight_sw_version,
        middleware_sw_version=identity.middleware_sw_version,
        os_sw_version=identity.os_sw_version,
        board_version=identity.board_version,
        flight_custom_version=bytes.fromhex(identity.flight_custom_version_hex),
        middleware_custom_version=bytes.fromhex(identity.middleware_custom_version_hex),
        os_custom_version=bytes.fromhex(identity.os_custom_version_hex),
        vendor_id=identity.vendor_id,
        product_id=identity.product_id,
        uid=0,
        uid2=bytes(18),
    )


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


class _FrameSource:
    def __init__(self) -> None:
        self.closed = False

    def __iter__(self):
        return iter(())

    def close(self) -> None:
        self.closed = True
        return None


class _FakePixhawk:
    def __init__(self, config: PixhawkReadOnlyConfig) -> None:
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
        self.autopilot_version_identity = _autopilot_version_identity()
        self.closed = False

    def snapshot(self, *, now_s: float):
        del now_s
        return object()

    def close(self) -> None:
        self.closed = True


def _write(tmp_path: Path, profile: RuntimeProfile) -> Path:
    path = tmp_path / "runtime-profile.json"
    path.write_text(
        json.dumps(sign_runtime_profile_document(profile, hmac_key=KEY, key_id="key-v1")),
        encoding="utf-8",
    )
    return path


def _identity() -> tuple[PixhawkReadOnlyConfig, str, str]:
    config = PixhawkReadOnlyConfig("udp:127.0.0.1:14550", expected_system_id=1)
    heartbeat = _FakePixhawk(config).heartbeat_identity
    return config, derive_vehicle_identity_hash(heartbeat), derive_endpoint_binding_hash(config)


def test_identity_hashes_are_stable_and_exclude_operational_state() -> None:
    first = PixhawkHeartbeatIdentity(1, 1, 12, 1, 3, 3)
    second = PixhawkHeartbeatIdentity(1, 1, 12, 1, 4, 3)
    assert derive_vehicle_identity_hash(first) == derive_vehicle_identity_hash(second)
    config = PixhawkReadOnlyConfig("udp:127.0.0.1:14550", baud=57_600)
    assert derive_endpoint_binding_hash(config) == derive_endpoint_binding_hash(
        config,
        resolved_endpoint="udp:127.0.0.1:14550",
    )
    runtime_identity = build_pixhawk_runtime_identity(
        _FakePixhawk(config),
        airframe_profile_hash=HASH_A,
    )
    assert runtime_identity.firmware_identity_hash == (_autopilot_version_identity().identity_hash)


def test_missing_heartbeat_cannot_become_a_valid_vehicle_binding() -> None:
    config = PixhawkReadOnlyConfig("udp:127.0.0.1:14550")

    class _NoHeartbeat:
        def __init__(self) -> None:
            self.config = config
            self.resolved_endpoint = "udp:127.0.0.1:14550"
            self.heartbeat_identity = PixhawkHeartbeatIdentity()

    identity = build_pixhawk_runtime_identity(_NoHeartbeat(), airframe_profile_hash=HASH_A)
    assert identity.vehicle_identity_hash is None


def test_firmware_identity_must_match_the_current_heartbeat_source() -> None:
    config = PixhawkReadOnlyConfig("udp:127.0.0.1:14550", expected_system_id=1)
    provider = _FakePixhawk(config)
    provider.autopilot_version_identity = PixhawkAutopilotVersionIdentity(
        source_system_id=1,
        source_component_id=42,
        capabilities=17,
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
    identity = build_pixhawk_runtime_identity(
        provider,
        airframe_profile_hash=HASH_A,
    )
    assert identity.vehicle_identity_hash is not None
    assert identity.firmware_identity_hash is None


def test_hil_launcher_always_returns_locked_read_only_backend(tmp_path, monkeypatch) -> None:
    config, vehicle_hash, endpoint_hash = _identity()
    monkeypatch.setattr(launcher_module, "PixhawkReadOnlyTelemetryProvider", _FakePixhawk)
    profile_path = _write(
        tmp_path,
        RuntimeProfile.hil(
            vehicle_identity_hash=vehicle_hash,
            firmware_identity_hash=_autopilot_version_identity().identity_hash,
            airframe_profile_hash=HASH_A,
            endpoint_binding_hash=endpoint_hash,
        ),
    )
    backend = ProfileBoundRuntimeLauncher(
        ProfileBoundRuntimeConfig(
            mode=RuntimeMode.HIL,
            profile_path=profile_path,
            hmac_key=KEY,
            expected_key_id="key-v1",
            airframe_profile_hash=HASH_A,
            scenario_id="scenario-1",
            scenario_manifest_hash=HASH_B,
            scenario_seed=7,
        )
    ).launch(frame_source=_FrameSource(), pixhawk_config=config)

    assert backend.descriptor.mode is RuntimeMode.HIL
    assert backend.profile_result.profile_valid is True
    assert backend.profile_result.flight_control_enabled is False
    assert backend.output_locked is True
    assert backend.flight_control_enabled is False
    assert not isinstance(backend.telemetry_provider, ProfileBoundFlightControlProvider)
    backend.close()


def test_real_observer_profile_stays_read_only_even_with_opt_in(tmp_path, monkeypatch) -> None:
    config, _vehicle_hash, _endpoint_hash = _identity()
    monkeypatch.setattr(launcher_module, "PixhawkReadOnlyTelemetryProvider", _FakePixhawk)
    profile_path = _write(tmp_path, RuntimeProfile.observer())
    backend = ProfileBoundRuntimeLauncher(
        ProfileBoundRuntimeConfig(
            mode=RuntimeMode.REAL,
            profile_path=profile_path,
            hmac_key=KEY,
            expected_key_id="key-v1",
            airframe_profile_hash=HASH_A,
            allow_flight_control_output=True,
        )
    ).launch(frame_source=_FrameSource(), pixhawk_config=config)

    assert backend.profile_result.profile_valid is True
    assert backend.profile_result.flight_control_enabled is False
    assert backend.output_locked is True
    assert not isinstance(backend.telemetry_provider, ProfileBoundFlightControlProvider)
    backend.close()


def test_real_flight_profile_requires_three_gate_passes(tmp_path, monkeypatch) -> None:
    config, vehicle_hash, endpoint_hash = _identity()
    clock = ManualClock(initial_monotonic_s=10.0)
    monkeypatch.setattr(launcher_module, "PixhawkReadOnlyTelemetryProvider", _FakePixhawk)
    profile_path = _write(
        tmp_path,
        RuntimeProfile.flight(
            vehicle_identity_hash=vehicle_hash,
            firmware_identity_hash=_autopilot_version_identity().identity_hash,
            airframe_profile_hash=HASH_A,
            signing_identity_hash=HASH_C,
            endpoint_binding_hash=endpoint_hash,
        ),
    )
    backend = ProfileBoundRuntimeLauncher(
        ProfileBoundRuntimeConfig(
            mode=RuntimeMode.REAL,
            profile_path=profile_path,
            hmac_key=KEY,
            expected_key_id="key-v1",
            airframe_profile_hash=HASH_A,
            signing_identity_hash=HASH_C,
            allow_flight_control_output=True,
        )
    ).launch(frame_source=_FrameSource(), pixhawk_config=config, clock=clock)

    assert backend.profile_result.profile_valid is True
    assert backend.profile_result.flight_control_enabled is True
    assert backend.profile_binding_attested is False
    assert backend.output_locked is True
    assert backend.flight_control_enabled is False
    assert isinstance(backend.telemetry_provider, ProfileBoundFlightControlProvider)
    provider = backend.telemetry_provider
    provider._connection = _QueuedConnection()
    provider._resolved_endpoint = config.endpoint
    provider.ingest_message(_heartbeat_message(), received_at_s=clock.monotonic())
    provider.ingest_message(_autopilot_version_message(), received_at_s=clock.monotonic())
    assert provider.refresh_profile_binding(now_s=clock.monotonic()) is True
    assert backend.profile_binding_attested is True
    assert backend.output_locked is False
    assert backend.flight_control_enabled is True

    clock.advance(config.stale_after_seconds + 0.01)
    assert backend.output_locked is True
    assert backend.flight_control_enabled is False
    assert provider.autopilot_version_identity is None

    provider.ingest_message(_heartbeat_message(), received_at_s=clock.monotonic())
    assert provider.refresh_profile_binding(now_s=clock.monotonic()) is False
    assert backend.output_locked is True
    provider.ingest_message(_autopilot_version_message(), received_at_s=clock.monotonic())
    assert provider.refresh_profile_binding(now_s=clock.monotonic()) is True
    assert backend.output_locked is False

    backend.close()
    assert backend.output_locked is True
    assert backend.flight_control_enabled is False


def test_profile_bound_provider_blocks_every_transmission_until_live_attestation() -> None:
    config, vehicle_hash, endpoint_hash = _identity()
    telemetry_config = PixhawkReadOnlyConfig(
        config.endpoint,
        expected_system_id=1,
        request_ranging_streams=True,
    )
    clock = ManualClock(initial_monotonic_s=10.0)
    profile = RuntimeProfile.flight(
        vehicle_identity_hash=vehicle_hash,
        firmware_identity_hash=_autopilot_version_identity().identity_hash,
        airframe_profile_hash=HASH_A,
        signing_identity_hash=HASH_C,
        endpoint_binding_hash=derive_endpoint_binding_hash(telemetry_config),
    )
    provider = ProfileBoundFlightControlProvider(
        PixhawkFlightControlConfig(telemetry_config),
        expected_profile=profile,
        clock=clock,
    )
    connection = _QueuedConnection(_heartbeat_message())
    provider._connection = connection
    provider._resolved_endpoint = telemetry_config.endpoint

    provider.snapshot(now_s=clock.monotonic())
    assert provider.profile_binding_attested is False
    assert provider.output_locked is True
    assert provider.messages_transmitted == 0
    assert connection.command_calls == []
    with pytest.raises(RuntimeError, match="firmware identity required"):
        provider.request_mode("OFFBOARD")
    assert connection.mode_calls == []

    connection.messages.append(_autopilot_version_message())
    provider.snapshot(now_s=clock.monotonic())
    assert provider.profile_binding_attested is True
    assert provider.output_locked is False
    assert provider.messages_transmitted > 0
    assert connection.command_calls
    diagnostics = provider.diagnostics(now_s=clock.monotonic())
    assert diagnostics["profile_binding_attested"] is True
    assert diagnostics["hardware_control_enabled"] is True
    assert diagnostics["output_locked"] is False


def test_output_command_cannot_be_the_first_profile_attestation_event() -> None:
    config, vehicle_hash, endpoint_hash = _identity()
    clock = ManualClock(initial_monotonic_s=10.0)
    provider = ProfileBoundFlightControlProvider(
        PixhawkFlightControlConfig(config),
        expected_profile=RuntimeProfile.flight(
            vehicle_identity_hash=vehicle_hash,
            firmware_identity_hash=_autopilot_version_identity().identity_hash,
            airframe_profile_hash=HASH_A,
            signing_identity_hash=HASH_C,
            endpoint_binding_hash=endpoint_hash,
        ),
        clock=clock,
    )
    connection = _QueuedConnection()
    provider._connection = connection
    provider._resolved_endpoint = config.endpoint
    provider.ingest_message(_heartbeat_message(), received_at_s=clock.monotonic())
    provider.ingest_message(_autopilot_version_message(), received_at_s=clock.monotonic())

    with pytest.raises(RuntimeError, match="not independently attested"):
        provider.request_mode("OFFBOARD")
    assert connection.mode_calls == []
    assert provider.refresh_profile_binding(now_s=clock.monotonic()) is True
    provider.request_mode("OFFBOARD")
    assert len(connection.mode_calls) == 1


def test_real_launcher_locks_legacy_firmwareless_flight_profile(tmp_path, monkeypatch) -> None:
    config, vehicle_hash, endpoint_hash = _identity()
    monkeypatch.setattr(launcher_module, "PixhawkReadOnlyTelemetryProvider", _FakePixhawk)
    profile_path = _write(
        tmp_path,
        RuntimeProfile.flight(
            vehicle_identity_hash=vehicle_hash,
            airframe_profile_hash=HASH_A,
            signing_identity_hash=HASH_C,
            endpoint_binding_hash=endpoint_hash,
        ),
    )
    backend = ProfileBoundRuntimeLauncher(
        ProfileBoundRuntimeConfig(
            mode=RuntimeMode.REAL,
            profile_path=profile_path,
            hmac_key=KEY,
            expected_key_id="key-v1",
            airframe_profile_hash=HASH_A,
            signing_identity_hash=HASH_C,
            allow_flight_control_output=True,
        )
    ).launch(frame_source=_FrameSource(), pixhawk_config=config)

    assert backend.profile_result.signature_valid is True
    assert backend.profile_result.identity_valid is False
    assert backend.profile_result.profile_valid is False
    assert backend.profile_result.flight_control_enabled is False
    assert backend.output_locked is True
    assert not isinstance(backend.telemetry_provider, ProfileBoundFlightControlProvider)
    assert "firmware identity binding is required" in backend.profile_result.reason
    backend.close()


def test_flight_provider_constructor_rejects_firmwareless_profile() -> None:
    config, vehicle_hash, endpoint_hash = _identity()
    with pytest.raises(ValueError, match="requires a firmware identity binding"):
        ProfileBoundFlightControlProvider(
            PixhawkFlightControlConfig(config),
            expected_profile=RuntimeProfile.flight(
                vehicle_identity_hash=vehicle_hash,
                airframe_profile_hash=HASH_A,
                signing_identity_hash=HASH_C,
                endpoint_binding_hash=endpoint_hash,
            ),
        )


def test_hil_config_rejects_scenario_or_output_mistakes(tmp_path) -> None:
    with pytest.raises(ValueError, match="HIL profile-bound runtime requires"):
        ProfileBoundRuntimeConfig(
            mode=RuntimeMode.HIL,
            profile_path=tmp_path / "profile.json",
            hmac_key=KEY,
            expected_key_id="key-v1",
            airframe_profile_hash=HASH_A,
        )
    with pytest.raises(ValueError, match="HIL runtime cannot enable"):
        ProfileBoundRuntimeConfig(
            mode=RuntimeMode.HIL,
            profile_path=tmp_path / "profile.json",
            hmac_key=KEY,
            expected_key_id="key-v1",
            airframe_profile_hash=HASH_A,
            scenario_id="scenario-1",
            scenario_manifest_hash=HASH_B,
            scenario_seed=1,
            allow_flight_control_output=True,
        )


def test_hil_config_rejects_real_signing_identity_and_normalizes_bounds(tmp_path) -> None:
    common = dict(
        mode=RuntimeMode.HIL,
        profile_path=tmp_path / "profile.json",
        hmac_key=KEY,
        expected_key_id="key-v1",
        airframe_profile_hash=HASH_A,
        scenario_id="scenario-1",
        scenario_manifest_hash=HASH_B,
        scenario_seed=1,
    )
    with pytest.raises(ValueError, match="cannot carry signing_identity_hash"):
        ProfileBoundRuntimeConfig(**common, signing_identity_hash=HASH_C)
    with pytest.raises(ValueError, match="scenario_manifest_hash"):
        ProfileBoundRuntimeConfig(**{**common, "scenario_manifest_hash": "bad"})
    with pytest.raises(ValueError, match="scenario_seed"):
        ProfileBoundRuntimeConfig(**{**common, "scenario_seed": -1})
    with pytest.raises(ValueError, match="backend_id"):
        ProfileBoundRuntimeConfig(**{**common, "backend_id": "   "})
    with pytest.raises(ValueError, match="rc_input_rate_hz"):
        ProfileBoundRuntimeConfig(**{**common, "rc_input_rate_hz": 1.0})
    with pytest.raises(ValueError, match="target_component_id"):
        ProfileBoundRuntimeConfig(**{**common, "target_component_id": True})


def test_unresolved_auto_endpoint_does_not_hash_the_literal_request() -> None:
    config = PixhawkReadOnlyConfig("auto")

    class _UnresolvedAuto:
        def __init__(self) -> None:
            self.config = config
            self.resolved_endpoint = None
            self.heartbeat_identity = PixhawkHeartbeatIdentity(
                source_system_id=1,
                source_component_id=1,
                autopilot_id=12,
                vehicle_type_id=1,
                system_status_id=4,
                mavlink_version=3,
            )

    observed = build_pixhawk_runtime_identity(_UnresolvedAuto(), airframe_profile_hash=HASH_A)
    assert observed.endpoint_binding_hash != derive_endpoint_binding_hash(config)


def test_failed_launch_closes_owned_sources(tmp_path, monkeypatch) -> None:
    config, _vehicle_hash, _endpoint_hash = _identity()

    class _InjectedFakePixhawk(_FakePixhawk):
        last: _InjectedFakePixhawk | None = None

        def __init__(self, injected_config: PixhawkReadOnlyConfig) -> None:
            super().__init__(injected_config)
            type(self).last = self

    monkeypatch.setattr(
        launcher_module,
        "PixhawkReadOnlyTelemetryProvider",
        _InjectedFakePixhawk,
    )
    profile_path = tmp_path / "unsigned.json"
    profile_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile": RuntimeProfile.hil(
                    vehicle_identity_hash=HASH_A,
                    airframe_profile_hash=HASH_A,
                    endpoint_binding_hash=HASH_B,
                ).to_mapping(),
            }
        ),
        encoding="utf-8",
    )

    frame_source = _FrameSource()
    with pytest.raises(Exception, match="signature"):
        ProfileBoundRuntimeLauncher(
            ProfileBoundRuntimeConfig(
                mode=RuntimeMode.HIL,
                profile_path=profile_path,
                hmac_key=KEY,
                expected_key_id="key-v1",
                airframe_profile_hash=HASH_A,
                scenario_id="scenario-1",
                scenario_manifest_hash=HASH_B,
                scenario_seed=1,
            )
        ).launch(frame_source=frame_source, pixhawk_config=config)
    assert _InjectedFakePixhawk.last is not None
    assert _InjectedFakePixhawk.last.closed is True
    # The launcher owns the source until it returns a backend.
    assert frame_source.closed is True


def test_build_pixhawk_runtime_identity_uses_provider_endpoint() -> None:
    config = PixhawkReadOnlyConfig("udp:127.0.0.1:14550")
    provider = _FakePixhawk(config)
    identity = build_pixhawk_runtime_identity(
        provider, airframe_profile_hash=HASH_A, signing_identity_hash=HASH_C
    )
    assert identity.airframe_profile_hash == HASH_A
    assert identity.signing_identity_hash == HASH_C
    assert identity.vehicle_identity_hash == derive_vehicle_identity_hash(
        provider.heartbeat_identity
    )
