from __future__ import annotations

import json

import pytest

from multidetect.runtime import RuntimeMode, RuntimeProfile
from multidetect.runtime_profiles import (
    RuntimeIdentity,
    RuntimeProfileDocumentError,
    load_runtime_profile_document,
    sign_runtime_profile_document,
)

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64
KEY = b"runtime-profile-test-key-material-32-bytes-minimum"


def _write(tmp_path, document: dict[str, object]):
    path = tmp_path / "runtime-profile.json"
    path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    return path


def _hil_document() -> dict[str, object]:
    return sign_runtime_profile_document(
        RuntimeProfile.hil(
            vehicle_identity_hash=HASH_A,
            airframe_profile_hash=HASH_B,
            endpoint_binding_hash=HASH_D,
        ),
        hmac_key=KEY,
        key_id="hil-key-v1",
    )


def _flight_document() -> dict[str, object]:
    return sign_runtime_profile_document(
        RuntimeProfile.flight(
            vehicle_identity_hash=HASH_A,
            firmware_identity_hash=HASH_E,
            airframe_profile_hash=HASH_B,
            signing_identity_hash=HASH_C,
            endpoint_binding_hash=HASH_D,
        ),
        hmac_key=KEY,
        key_id="flight-key-v1",
    )


def test_signed_hil_profile_is_valid_but_output_stays_locked(tmp_path) -> None:
    path = _write(tmp_path, _hil_document())
    result = load_runtime_profile_document(
        path,
        expected_mode=RuntimeMode.HIL,
        observed_identity=RuntimeIdentity(
            vehicle_identity_hash=HASH_A,
            airframe_profile_hash=HASH_B,
            endpoint_binding_hash=HASH_D,
        ),
        hmac_key=KEY,
        expected_key_id="hil-key-v1",
        allow_flight_control_output=True,
    )

    assert result.signature_valid is True
    assert result.identity_valid is True
    assert result.profile_valid is True
    assert result.flight_control_enabled is False
    assert result.output_locked is True
    assert "advisory-only" in result.reason


def test_real_flight_profile_requires_explicit_output_opt_in(tmp_path) -> None:
    path = _write(tmp_path, _flight_document())
    identity = RuntimeIdentity(
        vehicle_identity_hash=HASH_A,
        firmware_identity_hash=HASH_E,
        airframe_profile_hash=HASH_B,
        signing_identity_hash=HASH_C,
        endpoint_binding_hash=HASH_D,
    )

    locked = load_runtime_profile_document(
        path,
        expected_mode=RuntimeMode.REAL,
        observed_identity=identity,
        hmac_key=KEY,
        expected_key_id="flight-key-v1",
    )
    enabled = load_runtime_profile_document(
        path,
        expected_mode=RuntimeMode.REAL,
        observed_identity=identity,
        hmac_key=KEY,
        expected_key_id="flight-key-v1",
        allow_flight_control_output=True,
    )

    assert locked.profile_valid is True
    assert locked.flight_control_enabled is False
    assert "opt-in" in locked.reason
    assert enabled.flight_control_enabled is True
    assert enabled.output_locked is False


def test_legacy_real_flight_profile_is_readable_but_cannot_enable_output(tmp_path) -> None:
    document = sign_runtime_profile_document(
        RuntimeProfile.flight(
            vehicle_identity_hash=HASH_A,
            airframe_profile_hash=HASH_B,
            signing_identity_hash=HASH_C,
            endpoint_binding_hash=HASH_D,
        ),
        hmac_key=KEY,
        key_id="flight-key-v1",
    )
    path = _write(tmp_path, document)
    identity = RuntimeIdentity(
        vehicle_identity_hash=HASH_A,
        firmware_identity_hash=HASH_E,
        airframe_profile_hash=HASH_B,
        signing_identity_hash=HASH_C,
        endpoint_binding_hash=HASH_D,
    )

    diagnostic = load_runtime_profile_document(
        path,
        expected_mode=RuntimeMode.REAL,
        observed_identity=identity,
        hmac_key=KEY,
        expected_key_id="flight-key-v1",
    )
    output_attempt = load_runtime_profile_document(
        path,
        expected_mode=RuntimeMode.REAL,
        observed_identity=identity,
        hmac_key=KEY,
        expected_key_id="flight-key-v1",
        allow_flight_control_output=True,
    )

    assert diagnostic.profile.firmware_identity_hash is None
    assert diagnostic.profile_valid is True
    assert diagnostic.output_locked is True
    assert output_attempt.identity_valid is False
    assert output_attempt.profile_valid is False
    assert output_attempt.flight_control_enabled is False
    assert output_attempt.output_locked is True
    assert output_attempt.reason == (
        "runtime profile firmware identity binding is required; output remains locked"
    )


def test_firmware_requirement_flags_reject_non_booleans(tmp_path) -> None:
    path = _write(tmp_path, _flight_document())
    identity = RuntimeIdentity(
        vehicle_identity_hash=HASH_A,
        firmware_identity_hash=HASH_E,
        airframe_profile_hash=HASH_B,
        signing_identity_hash=HASH_C,
        endpoint_binding_hash=HASH_D,
    )

    with pytest.raises(TypeError, match="allow_flight_control_output must be boolean"):
        load_runtime_profile_document(
            path,
            expected_mode=RuntimeMode.REAL,
            observed_identity=identity,
            hmac_key=KEY,
            expected_key_id="flight-key-v1",
            allow_flight_control_output=1,  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="require_firmware_identity_binding must be boolean"):
        load_runtime_profile_document(
            path,
            expected_mode=RuntimeMode.REAL,
            observed_identity=identity,
            hmac_key=KEY,
            expected_key_id="flight-key-v1",
            require_firmware_identity_binding=1,  # type: ignore[arg-type]
        )


def test_real_profile_binding_mismatch_fails_closed(tmp_path) -> None:
    path = _write(tmp_path, _flight_document())
    result = load_runtime_profile_document(
        path,
        expected_mode=RuntimeMode.REAL,
        observed_identity=RuntimeIdentity(
            vehicle_identity_hash=HASH_A,
            airframe_profile_hash=HASH_B,
            signing_identity_hash="e" * 64,
            endpoint_binding_hash=HASH_D,
        ),
        hmac_key=KEY,
        expected_key_id="flight-key-v1",
        allow_flight_control_output=True,
    )

    assert result.signature_valid is True
    assert result.identity_valid is False
    assert result.profile_valid is False
    assert result.flight_control_enabled is False
    assert result.output_locked is True


def test_firmware_binding_preserves_legacy_readability_and_matches_when_present(
    tmp_path,
) -> None:
    legacy_path = _write(tmp_path, _hil_document())
    observed = RuntimeIdentity(
        vehicle_identity_hash=HASH_A,
        firmware_identity_hash=HASH_C,
        airframe_profile_hash=HASH_B,
        endpoint_binding_hash=HASH_D,
    )
    legacy = load_runtime_profile_document(
        legacy_path,
        expected_mode=RuntimeMode.HIL,
        observed_identity=observed,
        hmac_key=KEY,
        expected_key_id="hil-key-v1",
    )
    assert legacy.profile.firmware_identity_hash is None
    assert legacy.profile_valid is True
    strict_legacy = load_runtime_profile_document(
        legacy_path,
        expected_mode=RuntimeMode.HIL,
        observed_identity=observed,
        hmac_key=KEY,
        expected_key_id="hil-key-v1",
        require_firmware_identity_binding=True,
    )
    assert strict_legacy.identity_valid is False
    assert strict_legacy.profile_valid is False
    assert strict_legacy.output_locked is True
    assert "firmware identity binding is required" in strict_legacy.reason

    bound_document = sign_runtime_profile_document(
        RuntimeProfile.hil(
            vehicle_identity_hash=HASH_A,
            firmware_identity_hash=HASH_C,
            airframe_profile_hash=HASH_B,
            endpoint_binding_hash=HASH_D,
        ),
        hmac_key=KEY,
        key_id="hil-key-v1",
    )
    bound_path = _write(tmp_path, bound_document)
    matched = load_runtime_profile_document(
        bound_path,
        expected_mode=RuntimeMode.HIL,
        observed_identity=observed,
        hmac_key=KEY,
        expected_key_id="hil-key-v1",
    )
    mismatched = load_runtime_profile_document(
        bound_path,
        expected_mode=RuntimeMode.HIL,
        observed_identity=RuntimeIdentity(
            vehicle_identity_hash=HASH_A,
            firmware_identity_hash="e" * 64,
            airframe_profile_hash=HASH_B,
            endpoint_binding_hash=HASH_D,
        ),
        hmac_key=KEY,
        expected_key_id="hil-key-v1",
    )
    assert matched.profile_valid is True
    assert mismatched.identity_valid is False
    assert mismatched.profile_valid is False
    assert mismatched.output_locked is True


def test_hil_requires_signature_and_key_id(tmp_path) -> None:
    document = _hil_document()
    document.pop("signature_hmac_sha256")
    path = _write(tmp_path, document)
    with pytest.raises(RuntimeProfileDocumentError, match="signature is required"):
        load_runtime_profile_document(
            path,
            expected_mode=RuntimeMode.HIL,
            observed_identity=RuntimeIdentity(
                vehicle_identity_hash=HASH_A,
                airframe_profile_hash=HASH_B,
                endpoint_binding_hash=HASH_D,
            ),
        )


def test_profile_signature_tamper_is_rejected(tmp_path) -> None:
    document = _hil_document()
    profile = dict(document["profile"])
    profile["endpoint_binding_hash"] = "e" * 64
    document["profile"] = profile
    path = _write(tmp_path, document)
    with pytest.raises(RuntimeProfileDocumentError, match="signature mismatch"):
        load_runtime_profile_document(
            path,
            expected_mode=RuntimeMode.HIL,
            observed_identity=RuntimeIdentity(
                vehicle_identity_hash=HASH_A,
                airframe_profile_hash=HASH_B,
                endpoint_binding_hash=HASH_D,
            ),
            hmac_key=KEY,
            expected_key_id="hil-key-v1",
        )
