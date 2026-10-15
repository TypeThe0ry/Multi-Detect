"""Authentication, replay and flood controls for onboard mission intake."""

from __future__ import annotations

import copy
import json
import os

import pytest
from test_mission_supervisor import build, document

from multidetect.autonomy.mission_package import load_mission_package
from multidetect.autonomy.mission_security import (
    AuthenticationKey,
    canonical_auth_bytes,
    load_key,
    sign_document,
    verify_document,
)

cryptography = pytest.importorskip("cryptography")
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402


def _keys():
    private = Ed25519PrivateKey.generate()
    seed = private.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return seed, public


def test_signed_package_is_verified_before_admission(tmp_path):
    private, public = _keys()
    package = sign_document(document(), private)
    key_path = tmp_path / "operator.pub"
    key_path.write_bytes(public)
    assert load_mission_package(package, key_path=key_path).mission_id == "sortie-1"
    tampered = copy.deepcopy(package)
    tampered["home"] = [1, 1]
    with pytest.raises(ValueError, match="signature"):
        load_mission_package(tampered, key_path=key_path)


def test_commands_require_authentication_monotonic_sequence_and_fresh_time(tmp_path):
    private, public = _keys()
    supervisor, now, log = build(tmp_path)
    supervisor._security_key = public

    def command(sequence, **fields):
        payload = {"mission_id": "sortie-1", "type": "heartbeat", "sequence": sequence,
                   "timestamp": now[0], "timestamp_version": 1,
                   "timestamp_domain": "monotonic"}
        payload.update(fields)
        return sign_document(
            payload,
            private,
        )

    assert supervisor.ground_event(command(1))
    assert not supervisor.ground_event(command(1))  # replay
    wrong, _ = _keys()
    assert not supervisor.ground_event(sign_document(
        {"mission_id": "sortie-1", "type": "abort", "sequence": 2, "timestamp": now[0]}, wrong
    ))
    assert not supervisor.ground_event(command(3, timestamp=0))  # stale
    assert "operator" not in log.path.read_text(encoding="utf-8")


def test_signed_command_accepts_local_wall_clock_timestamp(tmp_path):
    private, public = _keys()
    supervisor, now, _ = build(tmp_path)
    supervisor._security_key = public
    local_wall = 1_800_000_000.0
    supervisor.wall_clock = lambda: local_wall
    event = sign_document(
        {
            "mission_id": "sortie-1",
            "type": "heartbeat",
            "sequence": 1,
            "timestamp": local_wall,
            "timestamp_version": 1,
            "timestamp_domain": "unix",
        },
        private,
    )
    assert supervisor.ground_event(event)


def test_command_flood_is_rate_limited_and_tampered_fields_fail(tmp_path):
    private, public = _keys()
    supervisor, now, _ = build(tmp_path)
    supervisor._security_key = public
    supervisor.rate_limit = 2

    def command(sequence):
        return sign_document(
            {"mission_id": "sortie-1", "type": "heartbeat", "sequence": sequence,
             "timestamp": now[0], "timestamp_version": 1,
             "timestamp_domain": "monotonic"}, private
        )

    assert supervisor.ground_event(command(1))
    tampered = command(2)
    tampered["type"] = "abort"
    assert not supervisor.ground_event(tampered)
    assert supervisor.ground_event(command(3))
    assert not supervisor.ground_event(command(4))


def test_hmac_fallback_contract_can_be_used_without_private_key_in_logs(tmp_path):
    key = b"throwaway-test-key" * 2
    package = sign_document(document(), key,)
    loaded = load_mission_package(package, key=key)
    assert loaded.mission_id == "sortie-1"
    assert key.decode() not in json.dumps(loaded.document)


def test_document_algorithm_cannot_downgrade_configured_ed25519_key():
    private, public = _keys()
    signed = sign_document({"mission_id": "m", "type": "heartbeat"}, private)
    # An attacker who knows the public key can calculate an HMAC, but the
    # locally configured Ed25519 key must never be repurposed as that secret.
    import hashlib
    import hmac

    forged = dict(signed, signature_alg="hmac-sha256")
    forged["signature"] = __import__("base64").b64encode(
        hmac.new(
            public,
            canonical_auth_bytes(forged, "signature", "signature_alg"),
            hashlib.sha256,
        ).digest()
    ).decode("ascii")
    assert not verify_document(forged, AuthenticationKey("ed25519", public, "default"))


def test_key_prefixes_are_strict_and_key_id_is_bound(tmp_path):
    private, public = _keys()
    path = tmp_path / "operator.pub"
    path.write_text(f'{{"algorithm":"ed25519","key_id":"operator-a","key":"ed25519-public:{public.hex()}"}}')
    configured = load_key(path)
    assert configured == AuthenticationKey("ed25519", public, "operator-a")
    signed = sign_document({"mission_id": "m"}, AuthenticationKey("ed25519", private, "operator-a"))
    assert verify_document(signed, configured)
    signed["key_id"] = "operator-b"
    assert not verify_document(signed, configured)
    path.write_text("hmac:not-a-valid-key")
    with pytest.raises(ValueError):
        load_key(path)
    path.write_text(json.dumps({"algorithm": "hmac-sha256", "key": (b"x" * 32).hex()}))
    with pytest.raises(ValueError, match="prefix"):
        load_key(path)


def test_flight_package_requires_signature_without_a_key(tmp_path):
    with pytest.raises(ValueError, match="operator key"):
        load_mission_package(document(), mode="flight")


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission checks are not available")
def test_secret_key_file_permissions_fail_closed(tmp_path):
    path = tmp_path / "operator.key"
    path.write_text("hmac:" + (b"x" * 32).hex())
    path.chmod(0o644)
    with pytest.raises(PermissionError):
        load_key(path)

def test_binary_key_with_whitespace_edge_bytes_is_not_stripped(tmp_path):
    # Raw 32-byte keys whose first/last byte is 0x09-0x0d or 0x20 used to lose that byte
    # when the key file was read, so a valid signature failed about 1 time in 20.
    whitespace = b" \t\n\r\x0b\x0c"
    for _ in range(5000):
        private, public = _keys()
        if public[0] in whitespace or public[-1] in whitespace:
            break
    else:  # pragma: no cover - probability ~ (1 - 12/256) ** 5000
        pytest.skip("no whitespace-edged key generated")
    key_path = tmp_path / "operator.pub"
    key_path.write_bytes(public)
    package = sign_document(document(), private)
    assert load_mission_package(package, key_path=key_path).mission_id == "sortie-1"
