"""Profile-bound HIL/REAL runtime launchers.

The live camera command historically constructed a Pixhawk provider directly.
That is useful for development, but it leaves the runtime identity boundary
outside the object that owns the frame/telemetry sources.  This module binds a
signed :class:`RuntimeProfile` to one Pixhawk endpoint and returns the same raw
sensor backend contract consumed by ``LiveMissionRunner``.

The launcher is deliberately small and side-effect aware:

* the profile document is verified before any flight-control provider is
  created;
* the first Pixhawk probe is read-only and is used only to derive identity;
* HIL always returns a read-only provider and a locked output decision;
* REAL creates the flight-control provider only after signature, identity and
  explicit opt-in all pass; and
* the output provider re-checks the binding immediately before every command.

No UI is defined here.  The resulting backend is injected into the existing
``LiveMissionRunner`` and therefore continues to use the native QGC Fly View
surface instead of introducing a simulator-specific window or theme.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .autonomy.serialization import (
    canonical_sha256,
    require_int,
    require_real,
    require_sha256,
    require_text,
)
from .clock import Clock, SystemClock
from .fixed_wing_aim_control import (
    PixhawkFlightControlConfig,
    PixhawkFlightControlProvider,
)
from .pixhawk import (
    PixhawkAutopilotVersionIdentity,
    PixhawkHeartbeatIdentity,
    PixhawkReadOnlyConfig,
    PixhawkReadOnlyTelemetryProvider,
)
from .runtime import RuntimeDescriptor, RuntimeMode, RuntimeProfile, RuntimeProfileId
from .runtime_profiles import (
    RuntimeIdentity,
    RuntimeProfileLoadResult,
    load_runtime_profile_document,
)
from .telemetry import TelemetryProvider
from .vision import FrameSource


def derive_vehicle_identity_hash(identity: PixhawkHeartbeatIdentity) -> str:
    """Derive a stable vehicle identity from non-volatile heartbeat fields.

    ``system_status_id`` is intentionally excluded: it changes during normal
    operation and is a qualification predicate, not an identity component.
    ``None`` values are retained in the canonical document so a missing
    heartbeat produces a deterministic hash that cannot accidentally match a
    real profile.
    """

    if not isinstance(identity, PixhawkHeartbeatIdentity):
        raise TypeError("identity must be a PixhawkHeartbeatIdentity")
    return canonical_sha256(
        {
            "schema_version": 1,
            "source_system_id": identity.source_system_id,
            "source_component_id": identity.source_component_id,
            "autopilot_id": identity.autopilot_id,
            "vehicle_type_id": identity.vehicle_type_id,
            "mavlink_version": identity.mavlink_version,
        }
    )


def derive_endpoint_binding_hash(
    config: PixhawkReadOnlyConfig,
    *,
    resolved_endpoint: str | None = None,
) -> str:
    """Derive an endpoint binding without including credentials or payloads."""

    if not isinstance(config, PixhawkReadOnlyConfig):
        raise TypeError("config must be a PixhawkReadOnlyConfig")
    endpoint = config.endpoint if resolved_endpoint is None else resolved_endpoint
    return canonical_sha256(
        {
            "schema_version": 1,
            "endpoint": require_text(endpoint, "endpoint").strip(),
            "baud": config.baud,
        }
    )


def derive_signing_identity_hash(signing_key: bytes) -> str:
    """Hash a MAVLink/signing key without exposing the key material."""

    if not isinstance(signing_key, bytes) or not signing_key:
        raise ValueError("signing_key must contain bytes")
    return hashlib.sha256(signing_key).hexdigest()


def signing_identity_hash_from_hex(value: str) -> str:
    """Hash a hex-encoded signing key read from an environment variable."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError("signing key hex cannot be empty")
    try:
        decoded = bytes.fromhex(value.strip())
    except ValueError as exc:
        raise ValueError("signing key hex must contain only hexadecimal bytes") from exc
    return derive_signing_identity_hash(decoded)


def build_pixhawk_runtime_identity(
    provider: PixhawkReadOnlyTelemetryProvider,
    *,
    airframe_profile_hash: str,
    signing_identity_hash: str | None = None,
) -> RuntimeIdentity:
    """Build the observed identity used by the signed profile gate."""

    if not isinstance(provider, PixhawkReadOnlyTelemetryProvider) and not all(
        hasattr(provider, attribute)
        for attribute in ("config", "heartbeat_identity", "resolved_endpoint")
    ):
        raise TypeError("provider must expose Pixhawk config and heartbeat identity")
    airframe_hash = require_sha256(airframe_profile_hash, "airframe_profile_hash")
    signing_hash = (
        None
        if signing_identity_hash is None
        else require_sha256(signing_identity_hash, "signing_identity_hash")
    )
    heartbeat = provider.heartbeat_identity
    heartbeat_complete = all(
        value is not None
        for value in (
            heartbeat.source_system_id,
            heartbeat.source_component_id,
            heartbeat.autopilot_id,
            heartbeat.vehicle_type_id,
            heartbeat.mavlink_version,
        )
    )
    # ``auto`` is only a request for discovery.  Never turn an unresolved
    # request into the literal endpoint hash for ``auto``: that would allow a
    # signed profile to appear bound before a real serial device was observed.
    resolved_endpoint = provider.resolved_endpoint
    if resolved_endpoint is None and provider.config.endpoint.strip().lower() == "auto":
        endpoint_hash = canonical_sha256(
            {
                "schema_version": 1,
                "endpoint": "<unresolved>",
                "requested_endpoint": "auto",
                "baud": provider.config.baud,
            }
        )
    else:
        endpoint_hash = derive_endpoint_binding_hash(
            provider.config,
            resolved_endpoint=resolved_endpoint,
        )
    autopilot_version = getattr(provider, "autopilot_version_identity", None)
    firmware_identity_hash = (
        autopilot_version.identity_hash
        if (
            isinstance(autopilot_version, PixhawkAutopilotVersionIdentity)
            and heartbeat_complete
            and autopilot_version.source_system_id == heartbeat.source_system_id
            and autopilot_version.source_component_id == heartbeat.source_component_id
        )
        else None
    )
    return RuntimeIdentity(
        # A missing heartbeat is represented as an absent identity instead of
        # hashing ``null`` values.  ``load_runtime_profile_document`` then
        # rejects the binding deterministically rather than allowing a profile
        # signed for an unobserved device to pass.
        vehicle_identity_hash=(
            derive_vehicle_identity_hash(heartbeat) if heartbeat_complete else None
        ),
        firmware_identity_hash=firmware_identity_hash,
        airframe_profile_hash=airframe_hash,
        endpoint_binding_hash=endpoint_hash,
        signing_identity_hash=signing_hash,
    )


def _profile_identity_matches(profile: RuntimeProfile, identity: RuntimeIdentity) -> bool:
    """Compare every profile-bound identity without exposing raw device identifiers."""

    if profile.profile_id is RuntimeProfileId.OBSERVER:
        return True
    required = (
        (profile.vehicle_identity_hash, identity.vehicle_identity_hash),
        (profile.airframe_profile_hash, identity.airframe_profile_hash),
        (profile.endpoint_binding_hash, identity.endpoint_binding_hash),
    )
    if profile.firmware_identity_hash is not None or profile.profile_id is RuntimeProfileId.FLIGHT:
        required = (
            *required,
            (profile.firmware_identity_hash, identity.firmware_identity_hash),
        )
    if profile.profile_id is RuntimeProfileId.FLIGHT:
        required = (*required, (profile.signing_identity_hash, identity.signing_identity_hash))
    return all(expected is not None and expected == observed for expected, observed in required)


class ProfileBoundFlightControlProvider(PixhawkFlightControlProvider):
    """Flight provider requiring its own live profile attestation before output.

    The launcher's read-only probe authorizes construction of this object, but
    that probe cannot authorize a different connection instance forever.  This
    provider therefore starts locked and must independently observe a fresh,
    qualified heartbeat plus the matching firmware identity.  Every outbound
    path, including telemetry/RC stream requests, shares the same live gate.
    """

    def __init__(
        self,
        config: PixhawkFlightControlConfig,
        *,
        expected_profile: RuntimeProfile,
        clock: Clock | None = None,
    ) -> None:
        super().__init__(config)
        if expected_profile.profile_id is not RuntimeProfileId.FLIGHT:
            raise ValueError("profile-bound flight provider requires a flight profile")
        if expected_profile.firmware_identity_hash is None:
            raise ValueError("profile-bound flight provider requires a firmware identity binding")
        selected_clock = clock or SystemClock()
        if not isinstance(selected_clock, Clock):
            raise TypeError("clock must implement Clock")
        self._expected_profile = expected_profile
        self._clock = selected_clock
        self._profile_binding_attested_at_s: float | None = None
        self._profile_binding_reason = (
            "control provider has not independently attested the runtime profile"
        )
        self._profile_binding_closed = False

    @property
    def profile_binding_attested(self) -> bool:
        if self._profile_binding_attested_at_s is None:
            return False
        now_s = self._clock.monotonic()
        failure, clear_firmware = self._profile_binding_failure(now_s=now_s)
        if failure is not None:
            self._revoke_profile_binding(
                failure,
                clear_firmware_identity=clear_firmware,
            )
            return False
        return True

    @property
    def profile_binding_attested_at_s(self) -> float | None:
        return self._profile_binding_attested_at_s if self.profile_binding_attested else None

    @property
    def profile_binding_reason(self) -> str:
        # Reading status can revoke a stale epoch, but it never creates a new
        # one.  Only ``snapshot``/``refresh_profile_binding`` can attest.
        _ = self.profile_binding_attested
        return self._profile_binding_reason

    @property
    def flight_control_enabled(self) -> bool:
        return self.profile_binding_attested

    @property
    def output_locked(self) -> bool:
        return not self.profile_binding_attested

    def refresh_profile_binding(self, *, now_s: float) -> bool:
        """Attest cached observations from this provider without transmitting."""

        if not math.isfinite(now_s) or now_s < 0.0:
            raise ValueError("profile binding time must be finite and non-negative")
        failure, clear_firmware = self._profile_binding_failure(now_s=now_s)
        if failure is not None:
            self._revoke_profile_binding(
                failure,
                clear_firmware_identity=clear_firmware,
            )
            return False
        if self._profile_binding_attested_at_s is None:
            self._profile_binding_attested_at_s = now_s
        self._profile_binding_reason = "current control provider binding attested"
        return True

    def ingest_message(self, message: Any, *, received_at_s: float | None = None) -> None:
        """Invalidate an identity epoch when a heartbeat resumes after a gap."""

        previous_heartbeat_s = self._last_heartbeat_s
        previous_heartbeat = self._heartbeat_identity
        message_type = (
            message.get_type() if hasattr(message, "get_type") else type(message).__name__
        )
        super().ingest_message(message, received_at_s=received_at_s)
        if message_type != "HEARTBEAT" or self._last_heartbeat_s == previous_heartbeat_s:
            return
        heartbeat_gap = (
            previous_heartbeat_s is not None
            and self._last_heartbeat_s is not None
            and self._last_heartbeat_s - previous_heartbeat_s > self.config.stale_after_seconds
        )
        heartbeat_changed = (
            previous_heartbeat.source_system_id is not None
            and self._heartbeat_identity != previous_heartbeat
        )
        if heartbeat_gap or heartbeat_changed:
            reason = (
                "Pixhawk heartbeat resumed after a stale link; fresh firmware identity required"
                if heartbeat_gap
                else "Pixhawk heartbeat identity changed; fresh firmware identity required"
            )
            self._revoke_profile_binding(reason, clear_firmware_identity=True)

    def snapshot(self, *, now_s: float):
        # Call the read-only implementation directly.  Its virtual stream hook
        # routes back through this class and therefore cannot transmit until
        # the just-received heartbeat/firmware pair has been attested.
        telemetry = PixhawkReadOnlyTelemetryProvider.snapshot(self, now_s=now_s)
        if self.refresh_profile_binding(now_s=now_s):
            self._ensure_rc_input_stream(now_s=now_s)
        return telemetry

    def diagnostics(self, *, now_s: float) -> dict[str, object]:
        attested = self.profile_binding_attested
        document = super().diagnostics(now_s=now_s)
        document.update(
            {
                "hardware_control_enabled": attested,
                "profile_binding_attested": attested,
                "profile_binding_attested_at_s": self._profile_binding_attested_at_s,
                "profile_binding_reason": self._profile_binding_reason,
                "output_locked": not attested,
            }
        )
        return document

    def close(self) -> None:
        self._profile_binding_closed = True
        self._revoke_profile_binding(
            "control provider is closed",
            clear_firmware_identity=True,
        )
        super().close()

    def _maybe_request_ranging_streams(self, *, now_s: float) -> None:
        if not self.refresh_profile_binding(now_s=now_s):
            return
        PixhawkReadOnlyTelemetryProvider._maybe_request_ranging_streams(
            self,
            now_s=now_s,
        )

    def _profile_binding_failure(self, *, now_s: float) -> tuple[str | None, bool]:
        if self._profile_binding_closed:
            return "control provider is closed", True
        if self._connection is None:
            return "control provider transport is not connected", False
        if self.transport_link_healthy(now_s=now_s) is not True:
            return "fresh Pixhawk heartbeat required for live profile binding", True
        if self.qualification.passed is not True:
            return "Pixhawk identity qualification is not satisfied", False
        firmware_observed_at_s = self.autopilot_version_observed_at_s
        if (
            firmware_observed_at_s is None
            or not math.isfinite(firmware_observed_at_s)
            or firmware_observed_at_s < 0.0
            or firmware_observed_at_s > now_s
        ):
            return "fresh firmware identity required for live profile binding", True
        identity = build_pixhawk_runtime_identity(
            self,
            airframe_profile_hash=self._expected_profile.airframe_profile_hash or "",
            signing_identity_hash=self._expected_profile.signing_identity_hash,
        )
        if not _profile_identity_matches(self._expected_profile, identity):
            return "runtime profile binding changed; flight-control output locked", False
        return None, False

    def _revoke_profile_binding(
        self,
        reason: str,
        *,
        clear_firmware_identity: bool,
    ) -> None:
        self._profile_binding_attested_at_s = None
        self._profile_binding_reason = reason
        if clear_firmware_identity:
            self._clear_autopilot_version_identity()

    def _ensure_profile_binding(self) -> None:
        # Output commands are never allowed to become the first attestation
        # event.  A prior receive-only snapshot must have established the epoch.
        if not self.profile_binding_attested:
            raise RuntimeError(self._profile_binding_reason)
        failure, clear_firmware = self._profile_binding_failure(now_s=self._clock.monotonic())
        if failure is not None:
            self._revoke_profile_binding(
                failure,
                clear_firmware_identity=clear_firmware,
            )
            raise RuntimeError(failure)

    def _qualified_connection(self) -> Any:
        connection = super()._qualified_connection()
        self._ensure_profile_binding()
        return connection


class ProfileBoundLiveBackend:
    """Raw sensor backend carrying a validated profile and output decision."""

    def __init__(
        self,
        *,
        descriptor: RuntimeDescriptor,
        clock: Clock,
        frame_source: FrameSource,
        telemetry_provider: TelemetryProvider,
        profile_result: RuntimeProfileLoadResult,
        observed_identity: RuntimeIdentity,
    ) -> None:
        if not isinstance(descriptor, RuntimeDescriptor):
            raise TypeError("descriptor must be a RuntimeDescriptor")
        if not isinstance(clock, Clock):
            raise TypeError("clock must implement Clock")
        if not isinstance(profile_result, RuntimeProfileLoadResult):
            raise TypeError("profile_result must be a RuntimeProfileLoadResult")
        if not isinstance(observed_identity, RuntimeIdentity):
            raise TypeError("observed_identity must be a RuntimeIdentity")
        self.descriptor = descriptor
        self.clock = clock
        self.frame_source = frame_source
        self._telemetry_provider = telemetry_provider
        self._flight_control_provider = (
            telemetry_provider
            if isinstance(telemetry_provider, ProfileBoundFlightControlProvider)
            else None
        )
        self.profile_result = profile_result
        self.observed_identity = observed_identity
        self._closed = False

    @property
    def telemetry_provider(self) -> TelemetryProvider:
        return self._telemetry_provider

    @property
    def output_locked(self) -> bool:
        if self._closed or self.profile_result.output_locked:
            return True
        provider = self._flight_control_provider
        return provider is None or provider.output_locked

    @property
    def flight_control_enabled(self) -> bool:
        if self._closed or not self.profile_result.flight_control_enabled:
            return False
        provider = self._flight_control_provider
        return provider is not None and provider.flight_control_enabled

    @property
    def profile_binding_attested(self) -> bool:
        provider = self._flight_control_provider
        return (
            not self._closed
            and self.profile_result.flight_control_enabled
            and provider is not None
            and provider.profile_binding_attested
        )

    @property
    def profile_binding_reason(self) -> str:
        if self._closed:
            return "profile-bound runtime backend is closed"
        if not self.profile_result.flight_control_enabled:
            return self.profile_result.reason
        provider = self._flight_control_provider
        if provider is None:
            return "authorized flight profile has no bound control provider"
        return provider.profile_binding_reason

    @property
    def input_hash(self) -> str:
        return self.descriptor.canonical_hash

    def replace_telemetry_provider(self, provider: TelemetryProvider) -> None:
        """Replace the outer telemetry view (for example zone evidence)."""

        if self._closed:
            raise RuntimeError("cannot replace telemetry on a closed backend")
        if not hasattr(provider, "snapshot"):
            raise TypeError("provider must implement TelemetryProvider")
        self._telemetry_provider = provider

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        errors: list[BaseException] = []
        for resource in (self.frame_source, self._telemetry_provider):
            close = getattr(resource, "close", None)
            if callable(close):
                try:
                    close()
                except BaseException as exc:  # pragma: no cover - uncommon transport cleanup.
                    errors.append(exc)
        if errors:
            raise errors[0]


@dataclass(frozen=True, slots=True)
class ProfileBoundRuntimeConfig:
    """Inputs required to launch a HIL or REAL profile-bound backend."""

    mode: RuntimeMode
    profile_path: Path
    hmac_key: bytes
    expected_key_id: str
    airframe_profile_hash: str
    signing_identity_hash: str | None = None
    allow_flight_control_output: bool = False
    scenario_id: str | None = None
    scenario_manifest_hash: str | None = None
    scenario_seed: int | None = None
    backend_id: str = "pixhawk-profile-bound"
    target_component_id: int = 1
    rc_input_rate_hz: float = 20.0

    def __post_init__(self) -> None:
        if self.mode not in {RuntimeMode.HIL, RuntimeMode.REAL}:
            raise ValueError("profile-bound launcher supports only HIL and REAL modes")
        if not isinstance(self.profile_path, Path):
            object.__setattr__(self, "profile_path", Path(self.profile_path))
        if not isinstance(self.hmac_key, bytes) or len(self.hmac_key) < 32:
            raise ValueError("hmac_key must contain at least 32 bytes")
        object.__setattr__(
            self,
            "expected_key_id",
            require_text(self.expected_key_id, "expected_key_id"),
        )
        object.__setattr__(
            self,
            "airframe_profile_hash",
            require_sha256(self.airframe_profile_hash, "airframe_profile_hash"),
        )
        if self.signing_identity_hash is not None:
            object.__setattr__(
                self,
                "signing_identity_hash",
                require_sha256(self.signing_identity_hash, "signing_identity_hash"),
            )
        if not isinstance(self.allow_flight_control_output, bool):
            raise ValueError("allow_flight_control_output must be boolean")
        object.__setattr__(self, "backend_id", require_text(self.backend_id, "backend_id"))
        if not isinstance(self.rc_input_rate_hz, (int, float)) or isinstance(
            self.rc_input_rate_hz, bool
        ):
            raise ValueError("rc_input_rate_hz must be a number in [2, 50]")
        object.__setattr__(
            self,
            "rc_input_rate_hz",
            require_real(self.rc_input_rate_hz, "rc_input_rate_hz", minimum=2.0, maximum=50.0),
        )
        if self.mode is RuntimeMode.HIL:
            if self.signing_identity_hash is not None:
                raise ValueError("HIL runtime cannot carry signing_identity_hash")
            if any(
                value is None
                for value in (
                    self.scenario_id,
                    self.scenario_manifest_hash,
                    self.scenario_seed,
                )
            ):
                raise ValueError(
                    "HIL profile-bound runtime requires scenario_id, "
                    "scenario_manifest_hash and scenario_seed"
                )
            if self.allow_flight_control_output:
                raise ValueError("HIL runtime cannot enable flight-control output")
            object.__setattr__(self, "scenario_id", require_text(self.scenario_id, "scenario_id"))
            object.__setattr__(
                self,
                "scenario_manifest_hash",
                require_sha256(self.scenario_manifest_hash, "scenario_manifest_hash"),
            )
            object.__setattr__(
                self,
                "scenario_seed",
                require_int(self.scenario_seed, "scenario_seed", minimum=0),
            )
        else:
            if any(
                value is not None
                for value in (
                    self.scenario_id,
                    self.scenario_manifest_hash,
                    self.scenario_seed,
                )
            ):
                raise ValueError("REAL profile-bound runtime cannot carry scenario provenance")
        if (
            isinstance(self.target_component_id, bool)
            or not isinstance(self.target_component_id, int)
            or not 1 <= self.target_component_id <= 255
        ):
            raise ValueError("target_component_id must be in [1, 255]")


class ProfileBoundRuntimeLauncher:
    """Create one profile-bound Pixhawk raw sensor backend."""

    def __init__(self, config: ProfileBoundRuntimeConfig) -> None:
        if not isinstance(config, ProfileBoundRuntimeConfig):
            raise TypeError("config must be a ProfileBoundRuntimeConfig")
        self.config = config

    def launch(
        self,
        *,
        frame_source: FrameSource,
        pixhawk_config: PixhawkReadOnlyConfig,
        clock: Clock | None = None,
    ) -> ProfileBoundLiveBackend:
        if not isinstance(pixhawk_config, PixhawkReadOnlyConfig):
            raise TypeError("pixhawk_config must be a PixhawkReadOnlyConfig")
        selected_clock = clock or SystemClock()
        if not isinstance(selected_clock, Clock):
            raise TypeError("clock must implement Clock")

        read_only = PixhawkReadOnlyTelemetryProvider(pixhawk_config)
        backend = None
        # Opening and probing the link is intentionally read-only.  No profile
        # decision can create a flight provider until this probe and signature
        # gate have completed.
        try:
            try:
                read_only.snapshot(now_s=selected_clock.monotonic())
            except Exception:
                # A missing heartbeat becomes a deterministic identity mismatch
                # and remains locked; the live loop can still retry telemetry.
                pass
            observed_identity = build_pixhawk_runtime_identity(
                read_only,
                airframe_profile_hash=self.config.airframe_profile_hash,
                signing_identity_hash=self.config.signing_identity_hash,
            )
            result = load_runtime_profile_document(
                self.config.profile_path,
                expected_mode=self.config.mode,
                observed_identity=observed_identity,
                hmac_key=self.config.hmac_key,
                expected_key_id=self.config.expected_key_id,
                require_signature=True,
                allow_flight_control_output=self.config.allow_flight_control_output,
                require_firmware_identity_binding=True,
            )

            descriptor = RuntimeDescriptor(
                mode=self.config.mode,
                profile=result.profile,
                backend_id=f"{self.config.backend_id}:{result.document_sha256[:16]}",
                airframe_profile_hash=self.config.airframe_profile_hash,
                scenario_id=self.config.scenario_id,
                scenario_manifest_hash=self.config.scenario_manifest_hash,
                scenario_seed=self.config.scenario_seed,
                deterministic=False,
                fault_injection_enabled=False,
            )

            telemetry_provider: TelemetryProvider = read_only
            if result.flight_control_enabled:
                if self.config.signing_identity_hash is None:
                    raise RuntimeError("valid flight profile requires signing_identity_hash")
                read_only.close()
                telemetry_provider = ProfileBoundFlightControlProvider(
                    PixhawkFlightControlConfig(
                        pixhawk_config,
                        target_component_id=self.config.target_component_id,
                        rc_input_rate_hz=self.config.rc_input_rate_hz,
                    ),
                    expected_profile=result.profile,
                    clock=selected_clock,
                )

            backend = ProfileBoundLiveBackend(
                descriptor=descriptor,
                clock=selected_clock,
                frame_source=frame_source,
                telemetry_provider=telemetry_provider,
                profile_result=result,
                observed_identity=observed_identity,
            )
            return backend
        except BaseException:
            # Launch owns the freshly-created source until a backend is
            # returned.  Close both sides on every failed gate so a bad profile
            # cannot leave a camera thread or MAVLink socket behind.
            try:
                read_only.close()
            finally:
                close = getattr(frame_source, "close", None)
                if callable(close):
                    close()
            raise


def launch_profile_bound_runtime(
    *,
    config: ProfileBoundRuntimeConfig,
    frame_source: FrameSource,
    pixhawk_config: PixhawkReadOnlyConfig,
    clock: Clock | None = None,
) -> ProfileBoundLiveBackend:
    """Functional convenience wrapper for one-shot CLI launchers."""

    return ProfileBoundRuntimeLauncher(config).launch(
        frame_source=frame_source,
        pixhawk_config=pixhawk_config,
        clock=clock,
    )


__all__ = [
    "ProfileBoundFlightControlProvider",
    "ProfileBoundLiveBackend",
    "ProfileBoundRuntimeConfig",
    "ProfileBoundRuntimeLauncher",
    "build_pixhawk_runtime_identity",
    "derive_endpoint_binding_hash",
    "derive_signing_identity_hash",
    "derive_vehicle_identity_hash",
    "launch_profile_bound_runtime",
    "signing_identity_hash_from_hex",
]
