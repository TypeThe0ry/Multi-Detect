"""Create and verify hash-anchored GR01 operator/reviewer trust registries."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from .autonomy.serialization import (
    AutonomyContractError,
    canonical_sha256,
    require_bool,
    require_sha256,
    require_text,
    strict_json_loads,
    strict_object,
)

REGISTRY_EVENT = "multidetect_gr01_review_trust_registry"
REGISTRY_SCHEMA_VERSION = 1
ROLES = frozenset({"source_operator", "reviewer"})


class Gr01ReviewTrustRegistryError(ValueError):
    """Raised when a GR01 review trust registry is invalid or untrusted."""


def _regular_file(value: str | Path, *, name: str) -> Path:
    requested = Path(value).expanduser()
    if requested.is_symlink() or not requested.is_file():
        raise Gr01ReviewTrustRegistryError(f"{name} must be a regular non-symlink file")
    return requested.resolve(strict=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _hex32(value: object, name: str) -> str:
    text = require_text(value, name).lower()
    try:
        decoded = bytes.fromhex(text)
    except ValueError as exc:
        raise Gr01ReviewTrustRegistryError(f"{name} must be 64 hexadecimal characters") from exc
    if len(text) != 64 or len(decoded) != 32:
        raise Gr01ReviewTrustRegistryError(f"{name} must be 64 hexadecimal characters")
    return text


def _utc(value: object, name: str) -> datetime:
    text = require_text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Gr01ReviewTrustRegistryError(f"{name} is not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise Gr01ReviewTrustRegistryError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _utc_text(value: datetime, name: str) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise Gr01ReviewTrustRegistryError(f"{name} must include a timezone")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _instant(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise Gr01ReviewTrustRegistryError(f"{name} must include a timezone")
    return value.astimezone(timezone.utc)


def _validate_principal(raw: object, index: int) -> dict[str, Any]:
    name = f"principals[{index}]"
    principal = strict_object(
        raw,
        name=name,
        required=frozenset(
            {
                "principal_id",
                "identity",
                "role",
                "key_id",
                "public_key_ed25519_hex",
                "valid_from_utc",
                "expires_at_utc",
                "revoked",
                "revoked_at_utc",
                "revocation_reason",
            }
        ),
    )
    principal_id = require_text(principal["principal_id"], f"{name}.principal_id")
    identity = require_text(principal["identity"], f"{name}.identity")
    role = require_text(principal["role"], f"{name}.role")
    if role not in ROLES:
        raise Gr01ReviewTrustRegistryError(f"{name}.role is unsupported")
    key_id = require_text(principal["key_id"], f"{name}.key_id")
    public_key = _hex32(principal["public_key_ed25519_hex"], f"{name}.public_key_ed25519_hex")
    valid_from = _utc(principal["valid_from_utc"], f"{name}.valid_from_utc")
    expires_at = _utc(principal["expires_at_utc"], f"{name}.expires_at_utc")
    if expires_at <= valid_from:
        raise Gr01ReviewTrustRegistryError(f"{name} validity window is empty")
    revoked = require_bool(principal["revoked"], f"{name}.revoked")
    revoked_at_raw = principal["revoked_at_utc"]
    reason_raw = principal["revocation_reason"]
    if revoked:
        revoked_at = _utc(revoked_at_raw, f"{name}.revoked_at_utc")
        reason = require_text(reason_raw, f"{name}.revocation_reason")
        if revoked_at < valid_from:
            raise Gr01ReviewTrustRegistryError(f"{name}.revoked_at_utc predates validity")
    else:
        if revoked_at_raw is not None or reason_raw is not None:
            raise Gr01ReviewTrustRegistryError(
                f"{name} non-revoked entry must have null revocation fields"
            )
        revoked_at = None
        reason = None
    return {
        "principal_id": principal_id,
        "identity": identity,
        "role": role,
        "key_id": key_id,
        "public_key_ed25519_hex": public_key,
        "valid_from_utc": _utc_text(valid_from, f"{name}.valid_from_utc"),
        "expires_at_utc": _utc_text(expires_at, f"{name}.expires_at_utc"),
        "revoked": revoked,
        "revoked_at_utc": (
            None if revoked_at is None else _utc_text(revoked_at, f"{name}.revoked_at_utc")
        ),
        "revocation_reason": reason,
    }


def _validate_document(document: object) -> dict[str, Any]:
    registry = strict_object(
        document,
        name="Gr01ReviewTrustRegistry",
        required=frozenset(
            {
                "event",
                "schema_version",
                "registry_id",
                "issued_at_utc",
                "valid_from_utc",
                "expires_at_utc",
                "principals",
                "policy",
                "claims",
                "canonical_hash",
            }
        ),
    )
    if registry["event"] != REGISTRY_EVENT or registry["schema_version"] != REGISTRY_SCHEMA_VERSION:
        raise Gr01ReviewTrustRegistryError("GR01 trust registry schema identity is invalid")
    registry_id = require_text(registry["registry_id"], "registry_id")
    try:
        if str(UUID(registry_id)) != registry_id:
            raise ValueError
    except ValueError as exc:
        raise Gr01ReviewTrustRegistryError("registry_id must be a canonical UUID") from exc
    issued_at = _utc(registry["issued_at_utc"], "issued_at_utc")
    valid_from = _utc(registry["valid_from_utc"], "valid_from_utc")
    expires_at = _utc(registry["expires_at_utc"], "expires_at_utc")
    if issued_at > valid_from or expires_at <= valid_from:
        raise Gr01ReviewTrustRegistryError("GR01 trust registry validity window is invalid")
    raw_principals = registry["principals"]
    if not isinstance(raw_principals, list) or len(raw_principals) < 2:
        raise Gr01ReviewTrustRegistryError("GR01 trust registry requires at least two principals")
    principals = [_validate_principal(item, index) for index, item in enumerate(raw_principals)]
    for field in ("principal_id", "key_id", "public_key_ed25519_hex"):
        values = [item[field] for item in principals]
        if len(values) != len(set(values)):
            raise Gr01ReviewTrustRegistryError(f"GR01 trust registry {field} values must be unique")
    if {item["role"] for item in principals} != ROLES:
        raise Gr01ReviewTrustRegistryError("GR01 trust registry must contain both required roles")
    policy = strict_object(
        registry["policy"],
        name="policy",
        required=frozenset(
            {
                "operator_reviewer_distinct_principals",
                "operator_reviewer_distinct_keys",
                "expired_keys_rejected",
                "revoked_keys_rejected",
            }
        ),
    )
    if any(require_bool(policy[name], f"policy.{name}") is not True for name in policy):
        raise Gr01ReviewTrustRegistryError("GR01 trust registry policy must remain fail-closed")
    claims = strict_object(
        registry["claims"],
        name="claims",
        required=frozenset(
            {
                "human_identity_verified",
                "hardware_flight_acceptance",
                "physical_release_enabled",
                "flight_control_enabled",
                "payload_release_requested",
            }
        ),
    )
    if (
        claims["human_identity_verified"] is not False
        or claims["hardware_flight_acceptance"] != "not_claimed"
        or any(
            claims[name] is not False
            for name in (
                "physical_release_enabled",
                "flight_control_enabled",
                "payload_release_requested",
            )
        )
    ):
        raise Gr01ReviewTrustRegistryError("GR01 trust registry claim boundary is invalid")
    claimed_hash = require_sha256(registry["canonical_hash"], "canonical_hash")
    unhashed = dict(registry)
    unhashed.pop("canonical_hash")
    if canonical_sha256(unhashed) != claimed_hash:
        raise Gr01ReviewTrustRegistryError("GR01 trust registry canonical hash does not match")
    return registry


def create_gr01_review_trust_registry(
    output_path: str | Path,
    *,
    registry_id: str,
    issued_at_utc: datetime,
    valid_from_utc: datetime,
    expires_at_utc: datetime,
    principals: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Create a write-new registry whose file SHA-256 is the external trust anchor."""

    output = Path(output_path).expanduser().resolve()
    if output.exists() or output.is_symlink():
        raise Gr01ReviewTrustRegistryError("GR01 trust registry output already exists")
    document: dict[str, Any] = {
        "event": REGISTRY_EVENT,
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "registry_id": require_text(registry_id, "registry_id"),
        "issued_at_utc": _utc_text(issued_at_utc, "issued_at_utc"),
        "valid_from_utc": _utc_text(valid_from_utc, "valid_from_utc"),
        "expires_at_utc": _utc_text(expires_at_utc, "expires_at_utc"),
        "principals": [dict(item) for item in principals],
        "policy": {
            "operator_reviewer_distinct_principals": True,
            "operator_reviewer_distinct_keys": True,
            "expired_keys_rejected": True,
            "revoked_keys_rejected": True,
        },
        "claims": {
            "human_identity_verified": False,
            "hardware_flight_acceptance": "not_claimed",
            "physical_release_enabled": False,
            "flight_control_enabled": False,
            "payload_release_requested": False,
        },
    }
    document["canonical_hash"] = canonical_sha256(document)
    _validate_document(document)
    _atomic_write(output, document)
    return document


def verify_gr01_review_trust_registry(
    registry_path: str | Path,
    *,
    expected_file_sha256: str,
    at_utc: datetime,
) -> dict[str, Any]:
    """Verify the external file anchor, closed registry, and registry lifetime."""

    path = _regular_file(registry_path, name="GR01 review trust registry")
    expected_hash = require_sha256(expected_file_sha256, "expected_trust_registry_sha256")
    actual_hash = _sha256(path)
    if actual_hash != expected_hash:
        raise Gr01ReviewTrustRegistryError("GR01 trust registry SHA-256 is not trusted")
    document = _validate_document(
        strict_json_loads(path.read_bytes(), name="GR01 review trust registry")
    )
    instant = _instant(at_utc, "at_utc")
    if not (
        _utc(document["valid_from_utc"], "valid_from_utc")
        <= instant
        < _utc(document["expires_at_utc"], "expires_at_utc")
    ):
        raise Gr01ReviewTrustRegistryError("GR01 trust registry is not active at review time")
    return {
        "path": path,
        "file_bytes": path.stat().st_size,
        "file_sha256": actual_hash,
        "document": document,
    }


def require_trusted_gr01_principal(
    verified_registry: Mapping[str, Any],
    *,
    role: str,
    key_id: str,
    identity: str,
    public_key_ed25519_hex: str,
    at_utc: datetime,
) -> dict[str, Any]:
    """Select one active, non-revoked principal and match its asserted identity/key."""

    requested_role = require_text(role, "role")
    requested_key_id = require_text(key_id, "key_id")
    principals = verified_registry["document"]["principals"]
    matches = [
        item
        for item in principals
        if item["role"] == requested_role and item["key_id"] == requested_key_id
    ]
    if len(matches) != 1:
        raise Gr01ReviewTrustRegistryError(
            f"GR01 trust registry has no unique active {requested_role} key: {requested_key_id}"
        )
    principal = matches[0]
    if principal["identity"] != require_text(identity, "identity") or principal[
        "public_key_ed25519_hex"
    ] != _hex32(public_key_ed25519_hex, "public_key_ed25519_hex"):
        raise Gr01ReviewTrustRegistryError(
            f"{requested_role} identity or public key does not match trusted registry"
        )
    instant = _instant(at_utc, "at_utc")
    if not (
        _utc(principal["valid_from_utc"], "principal.valid_from_utc")
        <= instant
        < _utc(principal["expires_at_utc"], "principal.expires_at_utc")
    ):
        raise Gr01ReviewTrustRegistryError(f"trusted {requested_role} key is expired or not active")
    if principal["revoked"] is True and instant >= _utc(
        principal["revoked_at_utc"], "principal.revoked_at_utc"
    ):
        raise Gr01ReviewTrustRegistryError(f"trusted {requested_role} key is revoked")
    return principal


def _atomic_write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create", help="create a write-new trust registry")
    create.add_argument("--principals-json", required=True, type=Path)
    create.add_argument("--registry-id", required=True)
    create.add_argument("--issued-at-utc", required=True)
    create.add_argument("--valid-from-utc", required=True)
    create.add_argument("--expires-at-utc", required=True)
    create.add_argument("--out", required=True, type=Path)
    verify = subparsers.add_parser("verify", help="verify a trust registry and hash anchor")
    verify.add_argument("registry", type=Path)
    verify.add_argument("--expected-file-sha256", required=True)
    verify.add_argument("--at-utc", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "create":
            principals_path = _regular_file(args.principals_json, name="principals JSON")
            principals = strict_json_loads(principals_path.read_bytes(), name="principals JSON")
            if not isinstance(principals, list):
                raise Gr01ReviewTrustRegistryError("principals JSON must contain an array")
            document = create_gr01_review_trust_registry(
                args.out,
                registry_id=args.registry_id,
                issued_at_utc=_utc(args.issued_at_utc, "issued_at_utc"),
                valid_from_utc=_utc(args.valid_from_utc, "valid_from_utc"),
                expires_at_utc=_utc(args.expires_at_utc, "expires_at_utc"),
                principals=principals,
            )
        else:
            result = verify_gr01_review_trust_registry(
                args.registry,
                expected_file_sha256=args.expected_file_sha256,
                at_utc=_utc(args.at_utc, "at_utc"),
            )
            document = result["document"]
    except (
        AutonomyContractError,
        Gr01ReviewTrustRegistryError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        print(f"GR01 review trust registry: {exc}")
        return 2
    print(json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
