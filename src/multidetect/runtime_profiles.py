"""Versioned RuntimeProfile documents and fail-closed HIL/REAL bindings.

The runtime descriptor already constrains which capability profile can run in
each mode.  This module adds the file/identity boundary used by HIL and REAL
launchers: a profile document is parsed once, its optional HMAC is verified,
the declared device/airframe/endpoint bindings are compared with the
observed identity, and only an explicitly enabled, fully-bound REAL profile
can ever expose flight-control output.  HIL remains advisory-only even when
its profile is valid.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .autonomy.serialization import (
    AutonomyContractError,
    canonical_json,
    require_schema_version,
    require_sha256,
    require_text,
    strict_object,
)
from .runtime import (
    RuntimeCapability,
    RuntimeMode,
    RuntimeProfile,
    RuntimeProfileId,
)

RUNTIME_PROFILE_DOCUMENT_SCHEMA_VERSION = 1
_MINIMUM_HMAC_KEY_BYTES = 32


class RuntimeProfileDocumentError(AutonomyContractError):
    """Raised when a runtime profile document or binding is invalid."""


def _require_hmac_key(value: bytes, *, name: str = "hmac_key") -> bytes:
    if not isinstance(value, bytes) or len(value) < _MINIMUM_HMAC_KEY_BYTES:
        raise RuntimeProfileDocumentError(
            f"{name} must contain at least {_MINIMUM_HMAC_KEY_BYTES} bytes"
        )
    return value


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    """Observed identities used to bind a profile to one endpoint."""

    airframe_profile_hash: str
    endpoint_binding_hash: str
    vehicle_identity_hash: str | None = None
    firmware_identity_hash: str | None = None
    signing_identity_hash: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "airframe_profile_hash",
            require_sha256(self.airframe_profile_hash, "airframe_profile_hash"),
        )
        object.__setattr__(
            self,
            "endpoint_binding_hash",
            require_sha256(self.endpoint_binding_hash, "endpoint_binding_hash"),
        )
        if self.vehicle_identity_hash is not None:
            object.__setattr__(
                self,
                "vehicle_identity_hash",
                require_sha256(self.vehicle_identity_hash, "vehicle_identity_hash"),
            )
        if self.firmware_identity_hash is not None:
            object.__setattr__(
                self,
                "firmware_identity_hash",
                require_sha256(self.firmware_identity_hash, "firmware_identity_hash"),
            )
        if self.signing_identity_hash is not None:
            object.__setattr__(
                self,
                "signing_identity_hash",
                require_sha256(self.signing_identity_hash, "signing_identity_hash"),
            )

    @classmethod
    def from_mapping(cls, raw: object) -> RuntimeIdentity:
        value = strict_object(
            raw,
            name="RuntimeIdentity",
            required=frozenset({"airframe_profile_hash", "endpoint_binding_hash"}),
            optional=frozenset(
                {
                    "vehicle_identity_hash",
                    "firmware_identity_hash",
                    "signing_identity_hash",
                }
            ),
        )
        return cls(
            airframe_profile_hash=require_sha256(
                value["airframe_profile_hash"], "airframe_profile_hash"
            ),
            endpoint_binding_hash=require_sha256(
                value["endpoint_binding_hash"], "endpoint_binding_hash"
            ),
            vehicle_identity_hash=(
                None
                if value.get("vehicle_identity_hash") is None
                else require_sha256(value["vehicle_identity_hash"], "vehicle_identity_hash")
            ),
            firmware_identity_hash=(
                None
                if value.get("firmware_identity_hash") is None
                else require_sha256(value["firmware_identity_hash"], "firmware_identity_hash")
            ),
            signing_identity_hash=(
                None
                if value.get("signing_identity_hash") is None
                else require_sha256(value["signing_identity_hash"], "signing_identity_hash")
            ),
        )

    def to_mapping(self) -> dict[str, str]:
        result = {
            "airframe_profile_hash": self.airframe_profile_hash,
            "endpoint_binding_hash": self.endpoint_binding_hash,
        }
        if self.vehicle_identity_hash is not None:
            result["vehicle_identity_hash"] = self.vehicle_identity_hash
        if self.firmware_identity_hash is not None:
            result["firmware_identity_hash"] = self.firmware_identity_hash
        if self.signing_identity_hash is not None:
            result["signing_identity_hash"] = self.signing_identity_hash
        return result


@dataclass(frozen=True, slots=True)
class RuntimeProfileLoadResult:
    """Validated profile plus the output decision made at the boundary."""

    profile: RuntimeProfile
    path: Path
    document_sha256: str
    signature_valid: bool
    identity_valid: bool
    profile_valid: bool
    flight_control_enabled: bool
    output_locked: bool
    reason: str

    @property
    def mode_capability(self) -> str:
        return self.profile.profile_id.value

    def to_mapping(self) -> dict[str, Any]:
        return {
            "profile": self.profile.to_mapping(),
            "path": str(self.path),
            "document_sha256": self.document_sha256,
            "signature_valid": self.signature_valid,
            "identity_valid": self.identity_valid,
            "profile_valid": self.profile_valid,
            "flight_control_enabled": self.flight_control_enabled,
            "output_locked": self.output_locked,
            "reason": self.reason,
        }


def _unsigned_document(*, profile: RuntimeProfile, key_id: str | None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": RUNTIME_PROFILE_DOCUMENT_SCHEMA_VERSION,
        "profile": profile.to_mapping(),
    }
    if key_id is not None:
        result["key_id"] = key_id
    return result


def sign_runtime_profile_document(
    profile: RuntimeProfile,
    *,
    hmac_key: bytes,
    key_id: str,
) -> dict[str, Any]:
    """Create a deterministic HMAC-authenticated runtime profile document."""

    if not isinstance(profile, RuntimeProfile):
        raise TypeError("profile must be a RuntimeProfile")
    key = _require_hmac_key(hmac_key)
    clean_key_id = require_text(key_id, "key_id")
    unsigned = _unsigned_document(profile=profile, key_id=clean_key_id)
    signature = hmac.new(
        key,
        canonical_json(unsigned).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {**unsigned, "signature_hmac_sha256": signature}


def _verify_signature(
    document: Mapping[str, Any],
    *,
    hmac_key: bytes | None,
    require_signature: bool,
    expected_key_id: str | None,
) -> bool:
    signature = document.get("signature_hmac_sha256")
    key_id = document.get("key_id")
    if signature is None:
        if require_signature:
            raise RuntimeProfileDocumentError("runtime profile signature is required")
        return False
    if hmac_key is None:
        raise RuntimeProfileDocumentError(
            "runtime profile signature is present but no HMAC key was supplied"
        )
    key = _require_hmac_key(hmac_key)
    supplied = require_text(signature, "signature_hmac_sha256").lower()
    if len(supplied) != 64 or any(char not in "0123456789abcdef" for char in supplied):
        raise RuntimeProfileDocumentError("signature_hmac_sha256 must be 64 hexadecimal characters")
    if not isinstance(key_id, str) or not key_id.strip():
        raise RuntimeProfileDocumentError("signed runtime profile requires key_id")
    if expected_key_id is not None and key_id.strip() != require_text(
        expected_key_id, "expected_key_id"
    ):
        raise RuntimeProfileDocumentError("runtime profile key_id does not match the expected key")
    unsigned = {key: value for key, value in document.items() if key != "signature_hmac_sha256"}
    expected = hmac.new(
        key,
        canonical_json(unsigned).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(supplied, expected):
        raise RuntimeProfileDocumentError("runtime profile signature mismatch")
    return True


def _identity_matches(profile: RuntimeProfile, identity: RuntimeIdentity) -> bool:
    if profile.profile_id is RuntimeProfileId.OBSERVER:
        # Read-only REAL observation intentionally carries no output bindings.
        # The signed document is still required by the HIL/REAL loader, while
        # the caller may separately bind the observed endpoint in its link
        # configuration.
        return True
    expected = {
        "airframe_profile_hash": profile.airframe_profile_hash,
        "endpoint_binding_hash": profile.endpoint_binding_hash,
        "vehicle_identity_hash": profile.vehicle_identity_hash,
        "firmware_identity_hash": profile.firmware_identity_hash,
        "signing_identity_hash": profile.signing_identity_hash,
    }
    observed = {
        "airframe_profile_hash": identity.airframe_profile_hash,
        "endpoint_binding_hash": identity.endpoint_binding_hash,
        "vehicle_identity_hash": identity.vehicle_identity_hash,
        "firmware_identity_hash": identity.firmware_identity_hash,
        "signing_identity_hash": identity.signing_identity_hash,
    }
    required_fields = {
        "vehicle_identity_hash",
        "airframe_profile_hash",
        "endpoint_binding_hash",
    }
    if profile.profile_id is RuntimeProfileId.FLIGHT:
        required_fields.add("signing_identity_hash")
    if profile.firmware_identity_hash is not None:
        required_fields.add("firmware_identity_hash")
    return all(
        expected[name] is not None
        and observed[name] is not None
        and expected[name] == observed[name]
        for name in required_fields
    )


def load_runtime_profile_document(
    path: str | Path,
    *,
    expected_mode: RuntimeMode | None = None,
    observed_identity: RuntimeIdentity | None = None,
    hmac_key: bytes | None = None,
    expected_key_id: str | None = None,
    require_signature: bool | None = None,
    allow_flight_control_output: bool = False,
    require_firmware_identity_binding: bool = False,
) -> RuntimeProfileLoadResult:
    """Load one profile and decide output authority without side effects.

    HIL profiles can be signed and identity-bound, but always return a locked
    flight-control decision.  REAL output is enabled only for a signed,
    identity-matching ``flight`` profile with a matching firmware identity and
    an explicit caller opt-in.  Generic diagnostic callers may leave
    ``require_firmware_identity_binding`` disabled to reopen legacy profiles;
    an actual REAL output request always enables the firmware requirement.
    """

    path = Path(path)
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise RuntimeProfileDocumentError(f"cannot read runtime profile: {path}") from exc
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeProfileDocumentError("runtime profile document is not valid JSON") from exc
    document = strict_object(
        raw,
        name="RuntimeProfileDocument",
        required=frozenset({"schema_version", "profile"}),
        optional=frozenset({"key_id", "signature_hmac_sha256"}),
    )
    require_schema_version(
        document["schema_version"], expected=RUNTIME_PROFILE_DOCUMENT_SCHEMA_VERSION
    )
    profile = RuntimeProfile.from_mapping(document["profile"])
    if expected_mode is not None and not isinstance(expected_mode, RuntimeMode):
        raise TypeError("expected_mode must be a RuntimeMode or None")
    if observed_identity is not None and not isinstance(observed_identity, RuntimeIdentity):
        raise TypeError("observed_identity must be a RuntimeIdentity or None")
    if not isinstance(allow_flight_control_output, bool):
        raise TypeError("allow_flight_control_output must be boolean")
    if not isinstance(require_firmware_identity_binding, bool):
        raise TypeError("require_firmware_identity_binding must be boolean")

    runtime_requires_binding = expected_mode in {RuntimeMode.HIL, RuntimeMode.REAL}
    signature_required = (
        runtime_requires_binding if require_signature is None else require_signature
    )
    signature_valid = _verify_signature(
        document,
        hmac_key=hmac_key,
        require_signature=signature_required,
        expected_key_id=expected_key_id,
    )

    if expected_mode is not None:
        allowed = {
            RuntimeMode.REPLAY: {RuntimeProfileId.REPLAY},
            RuntimeMode.SIM: {RuntimeProfileId.SIM},
            RuntimeMode.HIL: {RuntimeProfileId.HIL},
            RuntimeMode.REAL: {RuntimeProfileId.OBSERVER, RuntimeProfileId.FLIGHT},
        }[expected_mode]
        if profile.profile_id not in allowed:
            raise RuntimeProfileDocumentError(
                f"{profile.profile_id.value} profile cannot run in {expected_mode.value} mode"
            )

    firmware_identity_required = profile.profile_id in {
        RuntimeProfileId.HIL,
        RuntimeProfileId.FLIGHT,
    } and (
        require_firmware_identity_binding
        or (
            expected_mode is RuntimeMode.REAL
            and profile.profile_id is RuntimeProfileId.FLIGHT
            and allow_flight_control_output
        )
    )
    firmware_identity_reason: str | None = None
    if firmware_identity_required:
        if profile.firmware_identity_hash is None:
            firmware_identity_reason = (
                "runtime profile firmware identity binding is required; output remains locked"
            )
        elif observed_identity is None or observed_identity.firmware_identity_hash is None:
            firmware_identity_reason = (
                "observed firmware identity is required; output remains locked"
            )
        elif profile.firmware_identity_hash != observed_identity.firmware_identity_hash:
            firmware_identity_reason = (
                "runtime profile firmware identity does not match; output remains locked"
            )

    identity_valid = (
        True
        if not runtime_requires_binding
        else observed_identity is not None and _identity_matches(profile, observed_identity)
    ) and firmware_identity_reason is None
    profile_valid = (
        signature_valid and identity_valid if runtime_requires_binding else identity_valid
    )
    flight_control_enabled = (
        expected_mode is RuntimeMode.REAL
        and profile.profile_id is RuntimeProfileId.FLIGHT
        and RuntimeCapability.FLIGHT_CONTROL_OUTPUT in profile.capabilities
        and profile_valid
        and allow_flight_control_output
    )
    output_locked = not flight_control_enabled
    if firmware_identity_reason is not None:
        reason = firmware_identity_reason
    elif expected_mode is RuntimeMode.HIL:
        reason = "HIL profile is advisory-only; flight-control output remains locked"
    elif expected_mode is RuntimeMode.REAL and not profile_valid:
        reason = "REAL profile signature or device binding is invalid"
    elif expected_mode is RuntimeMode.REAL and not allow_flight_control_output:
        reason = "REAL profile is valid but output opt-in is not enabled"
    elif expected_mode is RuntimeMode.REAL and flight_control_enabled:
        reason = "REAL flight-control output explicitly enabled"
    else:
        reason = "profile loaded without flight-control capability"

    return RuntimeProfileLoadResult(
        profile=profile,
        path=path,
        document_sha256=hashlib.sha256(payload).hexdigest(),
        signature_valid=signature_valid,
        identity_valid=identity_valid,
        profile_valid=profile_valid,
        flight_control_enabled=flight_control_enabled,
        output_locked=output_locked,
        reason=reason,
    )


__all__ = [
    "RUNTIME_PROFILE_DOCUMENT_SCHEMA_VERSION",
    "RuntimeIdentity",
    "RuntimeProfileDocumentError",
    "RuntimeProfileLoadResult",
    "load_runtime_profile_document",
    "sign_runtime_profile_document",
]
