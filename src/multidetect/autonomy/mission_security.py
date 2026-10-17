"""Authentication helpers for mission packages and ground commands.

The verification algorithm is a property of the locally provisioned key. A
field in an untrusted JSON document can describe the algorithm for diagnostics,
but it can never select a different verification path.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NamedTuple

try:  # Optional dependency by design.
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    _ED25519 = True
except ImportError:  # pragma: no cover - exercised on minimal flight images.
    Ed25519PublicKey = None
    _ED25519 = False


SUPPORTED_ALGORITHMS = frozenset({"ed25519", "hmac-sha256"})
_DEFAULT_KEY_ID = "default"


class AuthenticationKey(NamedTuple):
    """Immutable locally configured authentication key."""

    algorithm: str
    key: bytes
    key_id: str


KeyMaterial = AuthenticationKey


def canonical_auth_bytes(value: Mapping[str, Any], *excluded: str) -> bytes:
    """Return deterministic UTF-8 JSON bytes for the signing contract."""

    excluded_set = set(excluded)
    def normalize(node: Any) -> Any:
        if isinstance(node, float):
            if not math.isfinite(node):
                raise ValueError("canonical JSON numbers must be finite")
            if node == 0.0:
                return 0
            if node.is_integer():
                if abs(node) > (2**53 - 1):
                    raise ValueError("canonical JSON integers must fit the exact JSON range")
                return int(node)
            return node
        if isinstance(node, bool):
            return node
        if isinstance(node, int) and abs(node) > (2**53 - 1):
            raise ValueError("canonical JSON integers must fit the exact JSON range")
        if isinstance(node, Mapping):
            return {key: normalize(item) for key, item in node.items()}
        if isinstance(node, (list, tuple)):
            return [normalize(item) for item in node]
        return node

    document = normalize({k: v for k, v in value.items() if k not in excluded_set})
    return json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _decode_encoded(text: str, *, label: str) -> bytes:
    """Decode exactly one hex or base64 representation; never fall back."""

    if not text:
        raise ValueError(f"{label} is empty")
    if all(char in "0123456789abcdefABCDEF" for char in text):
        if len(text) % 2:
            raise ValueError(f"{label} has an invalid hex length")
        try:
            return bytes.fromhex(text)
        except ValueError as exc:
            raise ValueError(f"{label} is not valid hex") from exc
    try:
        return base64.b64decode(text, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError(f"{label} is not valid hex or base64") from exc


def _validate_material(algorithm: str, material: bytes, *, private: bool = False) -> bytes:
    if algorithm == "ed25519":
        if not _ED25519:
            raise ValueError("Ed25519 support is unavailable")
        if len(material) != 32:
            raise ValueError("Ed25519 key material must be exactly 32 bytes")
    elif algorithm == "hmac-sha256":
        if not 16 <= len(material) <= 4096:
            raise ValueError("HMAC key must contain between 16 and 4096 bytes")
    else:
        raise ValueError("unsupported authentication algorithm")
    return bytes(material)


def _parse_prefixed(text: str) -> tuple[str, bytes, bool]:
    prefixes = {
        "ed25519-public:": ("ed25519", False),
        "ed25519-private:": ("ed25519", True),
        "hmac:": ("hmac-sha256", False),
    }
    lowered = text.lower()
    for prefix, (algorithm, private) in prefixes.items():
        if lowered.startswith(prefix):
            encoded = text[len(prefix) :].strip()
            material = _decode_encoded(encoded, label=prefix[:-1] + " key")
            return algorithm, _validate_material(algorithm, material, private=private), private
    if ":" in text:
        raise ValueError("unknown authentication key prefix")
    raise ValueError("authentication key is missing a supported prefix")


def _legacy_raw(raw: bytes) -> tuple[str, bytes, bool]:
    """Support old binary files while keeping malformed prefixed files strict."""

    material = bytes(raw)
    if len(material) == 32:
        return "ed25519", _validate_material("ed25519", material), False
    return "hmac-sha256", _validate_material("hmac-sha256", material), False


def _key_id(value: Any) -> str:
    if value is None:
        return _DEFAULT_KEY_ID
    if not isinstance(value, str) or not value.strip():
        raise ValueError("key_id must be non-empty text")
    return value.strip()


def _check_secret_permissions(path: Path, *, secret: bool) -> None:
    if not secret or os.name == "nt":
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise PermissionError("authentication secret key file is group/world readable")


def load_key(path: str | Path) -> AuthenticationKey:
    """Load an immutable, locally selected key without exposing key material."""

    key_path = Path(path)
    raw = key_path.read_bytes()
    if not raw:
        raise ValueError("authentication key file is empty")
    parsed_key_id: str | None = None
    # Decode ASCII files as text, but preserve arbitrary binary material byte for
    # byte. In particular, 0x09-0x0d and 0x20 are valid edge bytes in raw keys.
    try:
        text_source = raw.decode("ascii")
        stripped = text_source.strip()
        textual = any(
            stripped.lower().startswith(p)
            for p in ("ed25519-public:", "ed25519-private:", "hmac:")
        )
        # A raw fixed-size key wins over an accidentally base64-looking byte
        # string.  This preserves whitespace bytes at either edge.
        if not textual and len(raw) != 32:
            textual = len(stripped) % 2 == 0 and all(
                c in "0123456789abcdefABCDEF" for c in stripped
            )
            try:
                textual = textual or len(base64.b64decode(stripped, validate=True)) in {
                    16, 24, 32, 64
                }
            except (ValueError, binascii.Error):
                pass
        source = stripped.encode("ascii") if textual else raw
        if not textual and len(raw) == 32:
            # Keep the exact bytes, including ASCII whitespace at either edge.
            text_source = None
    except UnicodeDecodeError:
        text_source = None
        source = raw
    algorithm: str
    material: bytes
    private = False
    try:
        parsed = json.loads(text_source) if text_source is not None else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed = None
    if isinstance(parsed, dict):
        parsed_key_id = parsed.get("key_id")
        configured_algorithm = parsed.get("algorithm", parsed.get("alg"))
        if configured_algorithm is not None:
            if not isinstance(configured_algorithm, str):
                raise ValueError("authentication algorithm must be text")
            configured_algorithm = configured_algorithm.lower()
            if configured_algorithm not in SUPPORTED_ALGORITHMS:
                raise ValueError("unsupported authentication algorithm")
        selected_field = next(
            (field for field in ("public_key", "operator_key", "private_key", "hmac_key", "key")
             if isinstance(parsed.get(field), str)),
            None,
        )
        if selected_field is None:
            raise ValueError("authentication key document has no key material")
        value = parsed[selected_field]
        if not isinstance(value, str):
            raise ValueError("authentication key material must be text")
        pref_algorithm, material, private = _parse_prefixed(value)
        if configured_algorithm is not None and pref_algorithm != configured_algorithm:
            raise ValueError("authentication key algorithm mismatch")
        algorithm = pref_algorithm
    elif isinstance(parsed, (list, tuple)):
        raise ValueError("authentication key document must be an object")
    else:
        if text_source is None:
            algorithm, material, private = _legacy_raw(source)
        else:
            text = source.decode("ascii")
            prefixed = any(
                text.lower().startswith(prefix)
                for prefix in ("ed25519-public:", "ed25519-private:", "hmac:")
            )
            if prefixed or ":" in text:
                algorithm, material, private = _parse_prefixed(text.strip())
            else:
                raise ValueError("text authentication keys require a supported prefix")
    _check_secret_permissions(key_path, secret=(algorithm == "hmac-sha256" or private))
    if private:
        raise ValueError("verification key must be an Ed25519 public key")
    return AuthenticationKey(algorithm, material, _key_id(parsed_key_id))


def _coerce_key(key: bytes | AuthenticationKey, algorithm: str | None = None) -> AuthenticationKey:
    if isinstance(key, AuthenticationKey):
        if algorithm is not None and algorithm.lower() != key.algorithm:
            raise ValueError("authentication algorithm does not match configured key")
        return key
    if not isinstance(key, (bytes, bytearray, memoryview)):
        raise TypeError("authentication key must be bytes or AuthenticationKey")
    raw = bytes(key)
    selected = algorithm.lower() if isinstance(algorithm, str) else None
    if selected is None:
        selected = "ed25519" if _ED25519 and len(raw) == 32 else "hmac-sha256"
    if selected not in SUPPORTED_ALGORITHMS:
        raise ValueError("unsupported authentication algorithm")
    return AuthenticationKey(selected, _validate_material(selected, raw), _DEFAULT_KEY_ID)


def sign_bytes(
    payload: bytes,
    key: bytes | AuthenticationKey,
    *,
    algorithm: str | None = None,
) -> tuple[str, str]:
    configured = _coerce_key(key, algorithm)
    if configured.algorithm == "ed25519":
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        try:
            signature = Ed25519PrivateKey.from_private_bytes(configured.key).sign(payload)
        except Exception as exc:
            raise ValueError("Ed25519 signing requires a 32-byte private seed") from exc
    else:
        signature = hmac.new(configured.key, payload, hashlib.sha256).digest()
    return configured.algorithm, base64.b64encode(signature).decode("ascii")


def verify_bytes(
    payload: bytes,
    encoded: str,
    key: bytes | AuthenticationKey,
    *,
    algorithm: str | None = None,
) -> bool:
    try:
        configured = _coerce_key(key, algorithm)
        try:
            signature = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            signature = bytes.fromhex(encoded)
        if configured.algorithm == "ed25519":
            Ed25519PublicKey.from_public_bytes(configured.key).verify(signature, payload)
            return True
        expected = hmac.new(configured.key, payload, hashlib.sha256).digest()
        return hmac.compare_digest(signature, expected)
    except Exception:
        return False


def sign_document(
    document: Mapping[str, Any],
    key: bytes | AuthenticationKey,
    *,
    algorithm: str | None = None,
    excluded=("signature", "signature_alg"),
) -> dict[str, Any]:
    configured = _coerce_key(key, algorithm)
    result = dict(document)
    existing_key_id = result.get("key_id")
    if existing_key_id is not None and existing_key_id != configured.key_id:
        raise ValueError("document key_id does not match configured key")
    result["key_id"] = configured.key_id
    selected_algorithm, signature = sign_bytes(canonical_auth_bytes(result, *excluded), configured)
    result["signature_alg"], result["signature"] = selected_algorithm, signature
    return result


def verify_document(
    document: Mapping[str, Any],
    key: bytes | AuthenticationKey,
    *,
    excluded=("signature", "signature_alg"),
) -> bool:
    if not isinstance(document, Mapping):
        return False
    try:
        configured = _coerce_key(key)
        signature = document.get("signature", document.get("mac"))
        if isinstance(signature, Mapping):
            advertised_algorithm = signature.get("alg", signature.get("algorithm"))
            signature = signature.get("value", signature.get("signature"))
        else:
            advertised_algorithm = document.get("signature_alg", document.get("auth_alg"))
        if (
            advertised_algorithm != configured.algorithm
            or document.get("key_id") != configured.key_id
        ):
            return False
        if "signature_alg" in document and "auth_alg" in document:
            if document["signature_alg"] != document["auth_alg"]:
                return False
        if not isinstance(signature, str):
            return False
        omitted = tuple(set(excluded) | {"mac"})
        return verify_bytes(canonical_auth_bytes(document, *omitted), signature, configured)
    except Exception:
        return False


__all__ = [
    "AuthenticationKey", "KeyMaterial", "SUPPORTED_ALGORITHMS",
    "canonical_auth_bytes", "load_key", "sign_bytes", "verify_bytes",
    "sign_document", "verify_document",
]
