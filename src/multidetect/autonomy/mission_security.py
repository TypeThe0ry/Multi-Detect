"""Authentication helpers for mission packages and ground commands.

Ed25519 is preferred when :mod:`cryptography` is available.  The small HMAC
fallback keeps the onboard contract usable on minimal Python images; it uses a
pre-shared key and must be provisioned out of band.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

try:  # Optional dependency by design.
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    _ED25519 = True
except ImportError:  # pragma: no cover - exercised on minimal flight images.
    Ed25519PublicKey = None
    _ED25519 = False


def canonical_auth_bytes(value: Mapping[str, Any], *excluded: str) -> bytes:
    """Canonical JSON bytes with authentication fields removed."""
    document = {k: v for k, v in value.items() if k not in set(excluded)}
    return json.dumps(document, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _decode_key(raw: bytes) -> bytes:
    text = raw.strip()
    if not text:
        raise ValueError("authentication key file is empty")
    try:
        decoded = text.decode("ascii")
    except UnicodeDecodeError:
        return bytes(text)
    for prefix in ("ed25519-public:", "ed25519-private:", "hmac:"):
        if decoded.lower().startswith(prefix):
            decoded = decoded[len(prefix):].strip()
            break
    try:
        if len(decoded) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in decoded):
            return bytes.fromhex(decoded)
        return base64.b64decode(decoded, validate=True)
    except (ValueError, base64.binascii.Error):
        return bytes(text)


def load_key(path: str | Path) -> bytes:
    """Read key material without ever returning it in an exception message."""
    raw = Path(path).read_bytes()
    try:
        parsed = json.loads(raw.decode("utf-8"))
        if isinstance(parsed, dict):
            for field in ("public_key", "operator_key", "hmac_key", "key"):
                if isinstance(parsed.get(field), str):
                    return _decode_key(parsed[field].encode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    return _decode_key(raw)


def _algorithm(key: bytes, requested: str | None = None) -> str:
    if requested:
        requested = requested.lower()
        if requested in {"ed25519", "hmac-sha256"}:
            return requested
        raise ValueError("unsupported authentication algorithm")
    return "ed25519" if _ED25519 and len(key) == 32 else "hmac-sha256"


def sign_bytes(payload: bytes, key: bytes, *, algorithm: str | None = None) -> tuple[str, str]:
    algorithm = _algorithm(key, algorithm)
    if algorithm == "ed25519":
        # Signing is an operator-side operation; accept a raw 32-byte private key.
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        signature = Ed25519PrivateKey.from_private_bytes(key).sign(payload)
    else:
        signature = hmac.new(key, payload, hashlib.sha256).digest()
    return algorithm, base64.b64encode(signature).decode("ascii")


def verify_bytes(payload: bytes, encoded: str, key: bytes, *, algorithm: str | None = None) -> bool:
    try:
        try:
            signature = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error):
            signature = bytes.fromhex(encoded)
        algorithm = _algorithm(key, algorithm)
        if algorithm == "ed25519":
            try:
                Ed25519PublicKey.from_public_bytes(key).verify(signature, payload)
            except Exception:
                # Test/provisioning tools sometimes store the 32-byte private
                # seed.  Deriving its public key is safe and keeps verification
                # compatible with that file format.
                from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

                Ed25519PrivateKey.from_private_bytes(key).public_key().verify(signature, payload)
            return True
        return hmac.compare_digest(signature, hmac.new(key, payload, hashlib.sha256).digest())
    except Exception:
        return False


def sign_document(
    document: Mapping[str, Any],
    key: bytes,
    *,
    algorithm: str | None = None,
    excluded=("signature", "signature_alg"),
) -> dict[str, Any]:
    result = dict(document)
    algorithm, signature = sign_bytes(
        canonical_auth_bytes(result, *excluded), key, algorithm=algorithm
    )
    result["signature_alg"], result["signature"] = algorithm, signature
    return result


def verify_document(
    document: Mapping[str, Any], key: bytes, *, excluded=("signature", "signature_alg")
) -> bool:
    signature = document.get("signature", document.get("mac"))
    if isinstance(signature, Mapping):
        algorithm = signature.get("alg", signature.get("algorithm"))
        signature = signature.get("value", signature.get("signature"))
    else:
        algorithm = document.get("signature_alg", document.get("auth_alg"))
    if not isinstance(signature, str):
        return False
    omitted = tuple(set(excluded) | {"mac", "auth_alg"})
    return verify_bytes(
        canonical_auth_bytes(document, *omitted),
        signature,
        key,
        algorithm=algorithm,
    )


__all__ = [
    "canonical_auth_bytes", "load_key", "sign_bytes", "verify_bytes", "sign_document",
    "verify_document",
]
