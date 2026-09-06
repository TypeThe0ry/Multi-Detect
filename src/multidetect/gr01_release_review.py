"""Snapshot and independently review a copied GR01 final-session bundle."""

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
    canonical_json,
    canonical_sha256,
    require_bool,
    require_int,
    require_sha256,
    require_text,
    strict_json_loads,
    strict_object,
)
from .evidence_bundle_transfer import (
    create_evidence_transfer_inventory,
    verify_evidence_bundle_transfer,
    verify_evidence_bundle_transfer_receipt,
    verify_evidence_transfer_inventory,
)
from .gr01_bench import verify_gr01_link_bench_report
from .gr01_release_handoff import (
    _plan_projection,
    verify_gr01_release_session_handoff_portable,
)
from .gr01_release_session import (
    SESSION_EVENT,
    SESSION_SCHEMA_VERSION,
    verify_gr01_release_session_record,
)
from .gr01_review_trust_registry import (
    require_trusted_gr01_principal,
    verify_gr01_review_trust_registry,
)

REVIEW_SCHEMA_VERSION = 5
REVIEW_EVENT = "multidetect_gr01_release_session_portable_reviewed"
SOURCE_ATTESTATION_SCHEMA_VERSION = 1
SOURCE_ATTESTATION_EVENT = "multidetect_gr01_release_session_source_attested"
INVENTORY_NAME = "gr01-release-session-transfer-inventory.json"
SOURCE_ATTESTATION_NAME = "gr01-release-session-source-attestation.json"
RECORD_NAME = "gr01-release-session.json"
SIGNATURE_ALGORITHM = "Ed25519"


class Gr01ReleaseReviewError(ValueError):
    """Raised when a GR01 review bundle or receipt is invalid."""


def _regular_directory(value: str | Path, *, name: str) -> Path:
    requested = Path(value).expanduser()
    if requested.is_symlink() or not requested.is_dir():
        raise Gr01ReleaseReviewError(f"{name} must be a regular non-symlink directory")
    return requested.resolve(strict=True)


def _regular_file(value: str | Path, *, name: str) -> Path:
    requested = Path(value).expanduser()
    if requested.is_symlink() or not requested.is_file():
        raise Gr01ReleaseReviewError(f"{name} must be a regular non-symlink file")
    return requested.resolve(strict=True)


def _load_object(path: Path, *, name: str) -> dict[str, Any]:
    value = strict_json_loads(path.read_bytes(), name=name)
    if not isinstance(value, dict):
        raise Gr01ReleaseReviewError(f"{name} must contain a JSON object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _require_hex_32(value: object, name: str) -> str:
    text = require_text(value, name).lower()
    try:
        decoded = bytes.fromhex(text)
    except ValueError as exc:
        raise Gr01ReleaseReviewError(f"{name} must be 64 hexadecimal characters") from exc
    if len(text) != 64 or len(decoded) != 32:
        raise Gr01ReleaseReviewError(f"{name} must be 64 hexadecimal characters")
    return text


def _signing_types() -> tuple[Any, Any, type[Exception]]:
    try:
        from nacl.exceptions import BadSignatureError
        from nacl.signing import SigningKey, VerifyKey
    except ImportError as exc:
        raise Gr01ReleaseReviewError(
            "PyNaCl is required for signed GR01 review receipts; install .[security]"
        ) from exc
    return SigningKey, VerifyKey, BadSignatureError


def _signing_key_from_environment(variable_name: str, *, role: str) -> Any:
    name = require_text(variable_name, f"{role}_signing_key_environment")
    value = os.environ.get(name)
    if value is None:
        raise Gr01ReleaseReviewError(f"{role} signing key environment variable is missing: {name}")
    seed_hex = _require_hex_32(value, f"environment variable {name}")
    signing_key_type, _, _ = _signing_types()
    return signing_key_type(bytes.fromhex(seed_hex))


def _signed_payload_bytes(document: Mapping[str, Any]) -> bytes:
    return canonical_json(document).encode("utf-8")


def _inside(path: Path, *, root: Path, name: str) -> Path:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise Gr01ReleaseReviewError(f"{name} escapes review bundle root") from exc
    return path


def _bundle_source(
    projection: object,
    *,
    record_directory: Path,
    bundle_root: Path,
    name: str,
) -> Path:
    if not isinstance(projection, Mapping):
        raise Gr01ReleaseReviewError(f"{name} projection must be an object")
    value = projection.get("path")
    if not isinstance(value, str) or not value or "\\" in value:
        raise Gr01ReleaseReviewError(f"{name}.path must be a portable relative path")
    requested = Path(value)
    if requested.is_absolute() or ".." in requested.parts:
        raise Gr01ReleaseReviewError(f"{name}.path must remain inside the review bundle")
    path = _inside((record_directory / requested).resolve(), root=bundle_root, name=name)
    path = _regular_file(path, name=name)
    size = projection.get("bytes")
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or size < 1
        or size != path.stat().st_size
        or projection.get("sha256") != _sha256(path)
    ):
        raise Gr01ReleaseReviewError(f"{name} file identity does not match")
    return path


def snapshot_gr01_release_session_bundle(
    session_root: str | Path,
    inventory_out: str | Path,
    *,
    version_manifest_path: str | Path | None = None,
    bundle_id: str | None = None,
) -> dict[str, Any]:
    """Require the field verifier, then snapshot the complete session directory."""

    root = _regular_directory(session_root, name="GR01 session root")
    record = _regular_file(root / RECORD_NAME, name="GR01 release-session record")
    output = Path(inventory_out).expanduser().resolve()
    if output != root / INVENTORY_NAME:
        raise Gr01ReleaseReviewError(
            f"inventory output must be the canonical session file {INVENTORY_NAME}"
        )
    verification = verify_gr01_release_session_record(
        record,
        version_manifest_path=version_manifest_path,
    )
    if verification.get("gate_passed") is not True:
        detail = "; ".join(str(item) for item in verification.get("reasons", []))
        raise Gr01ReleaseReviewError(f"field session verification failed: {detail}")
    return create_evidence_transfer_inventory(
        root,
        output,
        bundle_id=bundle_id,
    )


def create_gr01_release_session_source_attestation(
    inventory: str | Path,
    attestation_out: str | Path,
    *,
    operator: str,
    operator_key_id: str,
    operator_signing_key_environment: str,
) -> dict[str, Any]:
    """Sign the canonical field inventory with a source-operator key."""

    output = Path(attestation_out).expanduser().resolve()
    if output.exists() or output.is_symlink():
        raise Gr01ReleaseReviewError("source attestation output already exists")
    verified_inventory = verify_evidence_transfer_inventory(inventory)
    try:
        output.relative_to(verified_inventory["path"].parent)
    except ValueError:
        pass
    else:
        raise Gr01ReleaseReviewError(
            "source attestation output must remain outside the inventoried session bundle"
        )
    operator_name = require_text(operator, "operator")
    key_id = require_text(operator_key_id, "operator_key_id")
    signing_key = _signing_key_from_environment(
        operator_signing_key_environment,
        role="operator",
    )
    public_key_hex = bytes(signing_key.verify_key).hex()
    document: dict[str, Any] = {
        "event": SOURCE_ATTESTATION_EVENT,
        "schema_version": SOURCE_ATTESTATION_SCHEMA_VERSION,
        "attested_at_utc": datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
        "bundle_id": verified_inventory["document"]["bundle_id"],
        "operator": {
            "identity": operator_name,
            "key_id": key_id,
            "public_key_ed25519_hex": public_key_hex,
        },
        "inventory": {
            "name": INVENTORY_NAME,
            "file_bytes": verified_inventory["file_bytes"],
            "file_sha256": verified_inventory["file_sha256"],
            "canonical_hash": verified_inventory["document"]["canonical_hash"],
            "bundle_content_hash": verified_inventory["document"]["bundle_content_hash"],
        },
        "claims": {
            "inventory_integrity_attested": True,
            "operator_key_possession_verified": True,
            "operator_human_identity_verified": False,
            "attestation_network_contacted": False,
            "hardware_flight_acceptance": "not_claimed",
            "physical_release_enabled": False,
            "flight_control_enabled": False,
            "payload_release_requested": False,
        },
    }
    signed_payload = _signed_payload_bytes(document)
    document["signature"] = {
        "algorithm": SIGNATURE_ALGORITHM,
        "signed_payload_sha256": hashlib.sha256(signed_payload).hexdigest(),
        "signature_ed25519_hex": signing_key.sign(signed_payload).signature.hex(),
    }
    document["canonical_hash"] = canonical_sha256(document)
    _atomic_write(output, document)
    verify_gr01_release_session_source_attestation(
        output,
        inventory,
        expected_operator_public_key_hex=public_key_hex,
    )
    return document


def verify_gr01_release_session_source_attestation(
    attestation: str | Path,
    inventory: str | Path,
    *,
    expected_operator_public_key_hex: str,
) -> dict[str, Any]:
    """Verify source signature and bind it to the supplied inventory bytes."""

    path = _regular_file(attestation, name="GR01 source attestation")
    document = strict_object(
        strict_json_loads(path.read_bytes(), name="GR01 source attestation"),
        name="Gr01ReleaseSessionSourceAttestation",
        required=frozenset(
            {
                "event",
                "schema_version",
                "attested_at_utc",
                "bundle_id",
                "operator",
                "inventory",
                "claims",
                "signature",
                "canonical_hash",
            }
        ),
    )
    if (
        document["event"] != SOURCE_ATTESTATION_EVENT
        or document["schema_version"] != SOURCE_ATTESTATION_SCHEMA_VERSION
    ):
        raise Gr01ReleaseReviewError("GR01 source attestation schema identity is invalid")
    attested_at = require_text(document["attested_at_utc"], "attested_at_utc")
    try:
        parsed = datetime.fromisoformat(attested_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Gr01ReleaseReviewError("attested_at_utc is not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise Gr01ReleaseReviewError("attested_at_utc must include a timezone")
    bundle_id = require_text(document["bundle_id"], "bundle_id")
    try:
        if str(UUID(bundle_id)) != bundle_id:
            raise ValueError
    except ValueError as exc:
        raise Gr01ReleaseReviewError("bundle_id must be a canonical UUID") from exc
    operator_projection = strict_object(
        document["operator"],
        name="operator",
        required=frozenset({"identity", "key_id", "public_key_ed25519_hex"}),
    )
    require_text(operator_projection["identity"], "operator.identity")
    require_text(operator_projection["key_id"], "operator.key_id")
    public_key_hex = _require_hex_32(
        operator_projection["public_key_ed25519_hex"],
        "operator.public_key_ed25519_hex",
    )
    expected_public_key_hex = _require_hex_32(
        expected_operator_public_key_hex,
        "expected_operator_public_key_hex",
    )
    if public_key_hex != expected_public_key_hex:
        raise Gr01ReleaseReviewError("source attestation operator public key is not trusted")
    verified_inventory = verify_evidence_transfer_inventory(inventory)
    inventory_projection = strict_object(
        document["inventory"],
        name="inventory",
        required=frozenset(
            {"name", "file_bytes", "file_sha256", "canonical_hash", "bundle_content_hash"}
        ),
    )
    expected_inventory = {
        "name": INVENTORY_NAME,
        "file_bytes": verified_inventory["file_bytes"],
        "file_sha256": verified_inventory["file_sha256"],
        "canonical_hash": verified_inventory["document"]["canonical_hash"],
        "bundle_content_hash": verified_inventory["document"]["bundle_content_hash"],
    }
    if (
        inventory_projection != expected_inventory
        or bundle_id != verified_inventory["document"]["bundle_id"]
    ):
        raise Gr01ReleaseReviewError("source attestation does not bind the supplied inventory")
    claims = strict_object(
        document["claims"],
        name="claims",
        required=frozenset(
            {
                "inventory_integrity_attested",
                "operator_key_possession_verified",
                "operator_human_identity_verified",
                "attestation_network_contacted",
                "hardware_flight_acceptance",
                "physical_release_enabled",
                "flight_control_enabled",
                "payload_release_requested",
            }
        ),
    )
    if (
        claims["inventory_integrity_attested"] is not True
        or claims["operator_key_possession_verified"] is not True
        or claims["operator_human_identity_verified"] is not False
        or claims["attestation_network_contacted"] is not False
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
        raise Gr01ReleaseReviewError("source attestation claim boundary is invalid")
    signature = strict_object(
        document["signature"],
        name="signature",
        required=frozenset({"algorithm", "signed_payload_sha256", "signature_ed25519_hex"}),
    )
    if signature["algorithm"] != SIGNATURE_ALGORITHM:
        raise Gr01ReleaseReviewError("source attestation signature algorithm is invalid")
    signed_document = dict(document)
    signed_document.pop("signature")
    signed_document.pop("canonical_hash")
    signed_payload = _signed_payload_bytes(signed_document)
    if (
        require_sha256(signature["signed_payload_sha256"], "signature.signed_payload_sha256")
        != hashlib.sha256(signed_payload).hexdigest()
    ):
        raise Gr01ReleaseReviewError("source attestation signed payload hash does not match")
    signature_hex = require_text(
        signature["signature_ed25519_hex"], "signature.signature_ed25519_hex"
    ).lower()
    try:
        signature_bytes = bytes.fromhex(signature_hex)
    except ValueError as exc:
        raise Gr01ReleaseReviewError("source attestation signature is not hexadecimal") from exc
    if len(signature_hex) != 128 or len(signature_bytes) != 64:
        raise Gr01ReleaseReviewError("source attestation signature must contain 64 bytes")
    _, verify_key_type, bad_signature_error = _signing_types()
    try:
        verify_key_type(bytes.fromhex(expected_public_key_hex)).verify(
            signed_payload,
            signature_bytes,
        )
    except bad_signature_error as exc:
        raise Gr01ReleaseReviewError("source attestation Ed25519 signature is invalid") from exc
    claimed_hash = require_sha256(document["canonical_hash"], "canonical_hash")
    unhashed = dict(document)
    unhashed.pop("canonical_hash")
    if claimed_hash != canonical_sha256(unhashed):
        raise Gr01ReleaseReviewError("source attestation canonical hash does not match")
    return document


def _portable_session_review(root: Path) -> dict[str, Any]:
    record_path = _regular_file(root / RECORD_NAME, name="GR01 release-session record")
    record = _load_object(record_path, name="GR01 release-session record")
    expected_record_fields = {
        "schema_version",
        "event",
        "session_id",
        "created_at_utc",
        "release_plan",
        "release_handoff",
        "bench_report",
        "release_manifest",
        "release_report",
        "claims",
        "gate_passed",
        "reasons",
    }
    if set(record) != expected_record_fields:
        raise Gr01ReleaseReviewError("GR01 release-session record fields do not match")
    if (
        record.get("schema_version") != SESSION_SCHEMA_VERSION
        or record.get("event") != SESSION_EVENT
        or record.get("gate_passed") is not True
        or record.get("reasons") != []
    ):
        raise Gr01ReleaseReviewError("GR01 release-session stored gate is not a clean pass")
    session_id = record.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise Gr01ReleaseReviewError("GR01 release-session session_id is invalid")

    directory = record_path.parent
    sources = {
        name: _bundle_source(
            record.get(name),
            record_directory=directory,
            bundle_root=root,
            name=name,
        )
        for name in (
            "release_plan",
            "release_handoff",
            "bench_report",
            "release_manifest",
            "release_report",
        )
    }
    if len(set(sources.values())) != 5:
        raise Gr01ReleaseReviewError("GR01 portable review requires five distinct sources")

    plan = _load_object(sources["release_plan"], name="GR01 source plan")
    portable_plan = _plan_projection(plan)
    handoff = _load_object(sources["release_handoff"], name="GR01 target handoff")
    handoff_verification = verify_gr01_release_session_handoff_portable(sources["release_handoff"])
    if handoff_verification.get("gate_passed") is not True:
        detail = "; ".join(str(item) for item in handoff_verification.get("reasons", []))
        raise Gr01ReleaseReviewError(f"portable handoff verification failed: {detail}")
    if handoff.get("source_equivalent") is not True or handoff.get("ready_to_execute") is not True:
        raise Gr01ReleaseReviewError("copied GR01 target handoff is not ready")
    handoff_plan = handoff.get("source_plan")
    target_identity = handoff.get("target_identity")
    if not isinstance(handoff_plan, Mapping) or not isinstance(target_identity, Mapping):
        raise Gr01ReleaseReviewError("GR01 target handoff identity is incomplete")
    handoff_plan_path = _bundle_source(
        handoff_plan,
        record_directory=sources["release_handoff"].parent,
        bundle_root=root,
        name="handoff source_plan",
    )
    if handoff_plan_path != sources["release_plan"]:
        raise Gr01ReleaseReviewError("GR01 target handoff references a different source plan")

    bench = _load_object(sources["bench_report"], name="GR01 bench report")
    bench_verification = verify_gr01_link_bench_report(bench, require_passed=True)
    if bench_verification.get("gate_passed") is not True:
        detail = "; ".join(str(item) for item in bench_verification.get("reasons", []))
        raise Gr01ReleaseReviewError(f"GR01 bench verification failed: {detail}")
    manifest = _load_object(sources["release_manifest"], name="release manifest")
    report = _load_object(sources["release_report"], name="release report")
    gr01_binding = manifest.get("real_gr01_operator_link")
    gr01_result = report.get("real_gr01_operator_link")
    if not isinstance(gr01_binding, Mapping) or not isinstance(gr01_result, Mapping):
        raise Gr01ReleaseReviewError("release evidence lacks the GR01 binding")
    if (
        gr01_binding.get("report_sha256") != _sha256(sources["bench_report"])
        or gr01_binding.get("report_bytes") != sources["bench_report"].stat().st_size
    ):
        raise Gr01ReleaseReviewError("release manifest references a different GR01 bench")
    if gr01_result.get("present") is not True or gr01_result.get("passed") is not True:
        raise Gr01ReleaseReviewError("release report GR01 binding is not a pass")

    thresholds = portable_plan["configuration"]["thresholds"]
    planned_configuration = {
        "host_address": portable_plan["configuration"]["host_address"],
        "port": portable_plan["configuration"]["port"],
        "hardware_id": portable_plan["configuration"]["hardware_id"],
        "expected_source_cidr": portable_plan["configuration"]["expected_source_cidr"],
        "minimum_round_trips": thresholds["minimum_round_trips"],
        "maximum_packet_loss_rate": thresholds["maximum_packet_loss_rate"],
        "maximum_ack_latency_p95_ms": thresholds["maximum_ack_latency_p95_ms"],
        "maximum_attempts": thresholds["maximum_attempts"],
        "maximum_evidence_age_s": thresholds["maximum_evidence_age_s"],
    }
    observed_configuration = {
        "host_address": bench.get("remote_host"),
        "port": bench.get("remote_port"),
        "hardware_id": bench.get("hardware_id"),
        "expected_source_cidr": bench.get("expected_source_cidr"),
        "minimum_round_trips": bench.get("requested_round_trips"),
        "maximum_packet_loss_rate": bench.get("thresholds", {}).get("maximum_packet_loss_rate"),
        "maximum_ack_latency_p95_ms": bench.get("thresholds", {}).get("maximum_ack_latency_p95_ms"),
        "maximum_attempts": bench.get("thresholds", {}).get("maximum_attempts"),
        "maximum_evidence_age_s": gr01_result.get("max_age_s"),
    }
    if planned_configuration != observed_configuration:
        raise Gr01ReleaseReviewError("source plan does not match copied bench and E7 result")

    release_plan_projection = record.get("release_plan")
    release_handoff_projection = record.get("release_handoff")
    bench_projection = record.get("bench_report")
    release_report_projection = record.get("release_report")
    if not all(
        isinstance(item, Mapping)
        for item in (
            release_plan_projection,
            release_handoff_projection,
            bench_projection,
            release_report_projection,
        )
    ):
        raise Gr01ReleaseReviewError("GR01 record source projections are incomplete")
    origin = portable_plan["source_identity"]
    target_parity = target_identity.get("release_parity")
    if not isinstance(target_parity, Mapping):
        raise Gr01ReleaseReviewError("GR01 target parity projection is incomplete")
    if (
        release_plan_projection.get("schema") != "multidetect.gr01-release-session-plan/v2"
        or release_plan_projection.get("schema_version") != 2
        or release_plan_projection.get("source_verified") is not True
        or release_plan_projection.get("configuration_matches_bench_and_e7") is not True
        or release_plan_projection.get("git_head") != origin["git_head"]
        or release_plan_projection.get("release_label") != origin["release_parity"]["release_label"]
        or release_plan_projection.get("safety_policy_hash")
        != origin["release_parity"]["safety_policy_hash"]
        or release_handoff_projection.get("git_head") != target_identity.get("git_head")
        or target_parity.get("release_label") != origin["release_parity"]["release_label"]
        or target_parity.get("safety_policy_hash") != origin["release_parity"]["safety_policy_hash"]
        or release_handoff_projection.get("schema") != "multidetect.gr01-release-session-handoff/v1"
        or release_handoff_projection.get("schema_version") != 1
        or release_handoff_projection.get("source_equivalent") is not True
        or release_handoff_projection.get("ready_to_execute") is not True
        or release_handoff_projection.get("environment_reverified") is not True
        or release_handoff_projection.get("configuration_matches_plan_bench_and_e7") is not True
        or release_handoff_projection.get("configuration_sha256")
        != handoff_plan.get("configuration_sha256")
    ):
        raise Gr01ReleaseReviewError("GR01 record source identity projections do not match")
    if (
        bench_projection.get("observed_at_utc") != bench.get("observed_at_utc")
        or bench_projection.get("hardware_id") != bench.get("hardware_id")
        or bench_projection.get("remote_host") != bench.get("remote_host")
        or bench_projection.get("remote_port") != bench.get("remote_port")
        or bench_projection.get("expected_source_cidr") != bench.get("expected_source_cidr")
        or bench_projection.get("local_source_address") != bench.get("local_source_address")
    ):
        raise Gr01ReleaseReviewError("GR01 record bench projection does not match")
    if (
        release_report_projection.get("gate_passed") != report.get("gate_passed")
        or release_report_projection.get("release_allowed") != report.get("release_allowed")
        or release_report_projection.get("blocker_count")
        != len(report.get("release_blocked_reasons", []))
        or release_report_projection.get("real_gr01_operator_link_passed") is not True
        or release_report_projection.get("age_s") != gr01_result.get("age_s")
        or release_report_projection.get("max_age_s") != gr01_result.get("max_age_s")
    ):
        raise Gr01ReleaseReviewError("GR01 record release-report projection does not match")
    expected_claims = {
        "gr01_binding_verified": True,
        "handoff_binding_verified": True,
        "overall_release_allowed": report.get("release_allowed") is True,
        "hardware_flight_acceptance": "not_claimed",
        "physical_release_enabled": False,
        "flight_control_enabled": False,
        "payload_release_requested": False,
    }
    if record.get("claims") != expected_claims:
        raise Gr01ReleaseReviewError("GR01 record claim boundary does not match")

    return {
        "record_path": RECORD_NAME,
        "record_bytes": record_path.stat().st_size,
        "record_sha256": _sha256(record_path),
        "session_id": session_id,
        "session_schema_version": SESSION_SCHEMA_VERSION,
        "source_count": 5,
        "five_source_hashes_verified": True,
        "portable_handoff_verified": True,
        "bench_recomputed": True,
        "configuration_matches_plan_bench_and_e7": True,
        "release_manifest_gr01_binding_matched": True,
        "stored_release_gate_passed": report.get("gate_passed") is True,
        "stored_release_allowed": report.get("release_allowed") is True,
        "stored_release_blocker_count": len(report.get("release_blocked_reasons", [])),
    }


def review_gr01_release_session_bundle(
    inventory: str | Path,
    source_attestation: str | Path,
    destination_root: str | Path,
    transfer_receipt_out: str | Path,
    review_out: str | Path,
    *,
    expected_inventory_sha256: str,
    trust_registry: str | Path,
    expected_trust_registry_sha256: str,
    reviewer: str,
    reviewer_key_id: str,
    reviewer_signing_key_environment: str,
) -> dict[str, Any]:
    """Verify a hash-anchored copy and save a portable semantic review receipt."""

    root = _regular_directory(destination_root, name="destination GR01 session root")
    review_path = Path(review_out).expanduser().resolve()
    transfer_path = Path(transfer_receipt_out).expanduser().resolve()
    for path, name in ((review_path, "review output"), (transfer_path, "transfer receipt")):
        if path.exists() or path.is_symlink():
            raise Gr01ReleaseReviewError(f"{name} already exists")
        try:
            path.relative_to(root)
        except ValueError:
            pass
        else:
            raise Gr01ReleaseReviewError(f"{name} must remain outside the copied bundle")
    expected_inventory_hash = require_sha256(
        expected_inventory_sha256,
        "expected_inventory_sha256",
    )
    verified_inventory = verify_evidence_transfer_inventory(inventory)
    if verified_inventory["file_sha256"] != expected_inventory_hash:
        raise Gr01ReleaseReviewError("transfer inventory SHA-256 does not match trusted value")
    reviewed_at = datetime.now(timezone.utc)
    reviewer_name = require_text(reviewer, "reviewer")
    key_id = require_text(reviewer_key_id, "reviewer_key_id")
    verified_registry = verify_gr01_review_trust_registry(
        trust_registry,
        expected_file_sha256=expected_trust_registry_sha256,
        at_utc=reviewed_at,
    )
    source_path = _regular_file(source_attestation, name="GR01 source attestation")
    untrusted_source = _load_object(source_path, name="GR01 source attestation")
    source_attested_at_text = require_text(
        untrusted_source.get("attested_at_utc"),
        "attested_at_utc",
    )
    try:
        source_attested_at = datetime.fromisoformat(source_attested_at_text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Gr01ReleaseReviewError("attested_at_utc is not an ISO-8601 timestamp") from exc
    if source_attested_at.tzinfo is None or source_attested_at.utcoffset() is None:
        raise Gr01ReleaseReviewError("attested_at_utc must include a timezone")
    if source_attested_at > reviewed_at:
        raise Gr01ReleaseReviewError("source attestation cannot postdate the portable review")
    source_time_registry = verify_gr01_review_trust_registry(
        trust_registry,
        expected_file_sha256=expected_trust_registry_sha256,
        at_utc=source_attested_at,
    )
    untrusted_operator = strict_object(
        untrusted_source.get("operator"),
        name="operator",
        required=frozenset({"identity", "key_id", "public_key_ed25519_hex"}),
    )
    trusted_operator = require_trusted_gr01_principal(
        source_time_registry,
        role="source_operator",
        key_id=untrusted_operator["key_id"],
        identity=untrusted_operator["identity"],
        public_key_ed25519_hex=untrusted_operator["public_key_ed25519_hex"],
        at_utc=source_attested_at,
    )
    source = verify_gr01_release_session_source_attestation(
        source_path,
        inventory,
        expected_operator_public_key_hex=trusted_operator["public_key_ed25519_hex"],
    )
    signing_key = _signing_key_from_environment(
        reviewer_signing_key_environment,
        role="reviewer",
    )
    public_key_hex = bytes(signing_key.verify_key).hex()
    source_operator = source["operator"]
    if (
        reviewer_name == source_operator["identity"]
        or key_id == source_operator["key_id"]
        or public_key_hex == source_operator["public_key_ed25519_hex"]
    ):
        raise Gr01ReleaseReviewError("source operator and reviewer roles and keys must be distinct")
    trusted_reviewer = require_trusted_gr01_principal(
        verified_registry,
        role="reviewer",
        key_id=key_id,
        identity=reviewer_name,
        public_key_ed25519_hex=public_key_hex,
        at_utc=reviewed_at,
    )
    require_trusted_gr01_principal(
        verified_registry,
        role="source_operator",
        key_id=source_operator["key_id"],
        identity=source_operator["identity"],
        public_key_ed25519_hex=source_operator["public_key_ed25519_hex"],
        at_utc=reviewed_at,
    )
    transfer = verify_evidence_bundle_transfer(inventory, root, transfer_path)
    verify_evidence_bundle_transfer_receipt(transfer_path)
    semantic = _portable_session_review(root)
    document: dict[str, Any] = {
        "event": REVIEW_EVENT,
        "schema_version": REVIEW_SCHEMA_VERSION,
        "reviewed_at_utc": reviewed_at.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "bundle_id": transfer["bundle_id"],
        "reviewer": {
            "identity": reviewer_name,
            "key_id": key_id,
            "public_key_ed25519_hex": public_key_hex,
        },
        "trust_registry": {
            "registry_id": verified_registry["document"]["registry_id"],
            "file_bytes": verified_registry["file_bytes"],
            "file_sha256": verified_registry["file_sha256"],
            "expected_file_sha256": require_sha256(
                expected_trust_registry_sha256,
                "expected_trust_registry_sha256",
            ),
            "canonical_hash": verified_registry["document"]["canonical_hash"],
            "operator_principal_id": trusted_operator["principal_id"],
            "reviewer_principal_id": trusted_reviewer["principal_id"],
            "active_at_review": True,
            "revocation_checked": True,
        },
        "source_attestation": {
            "attested_at_utc": source_attested_at_text,
            "file_bytes": source_path.stat().st_size,
            "file_sha256": _sha256(source_path),
            "canonical_hash": source["canonical_hash"],
            "operator_identity": source_operator["identity"],
            "operator_key_id": source_operator["key_id"],
            "operator_public_key_ed25519_hex": source_operator["public_key_ed25519_hex"],
            "inventory_signature_verified": True,
        },
        "transfer": {
            "expected_inventory_file_sha256": expected_inventory_hash,
            "inventory_file_bytes": verified_inventory["file_bytes"],
            "inventory_file_sha256": verified_inventory["file_sha256"],
            "inventory_hash_matches_expected": True,
            "inventory_canonical_hash": verified_inventory["document"]["canonical_hash"],
            "transfer_receipt_file_bytes": transfer_path.stat().st_size,
            "transfer_receipt_file_sha256": _sha256(transfer_path),
            "transfer_receipt_canonical_hash": transfer["canonical_hash"],
            "bundle_content_hash": transfer["destination_bundle_content_hash"],
            "byte_exact_copy": True,
        },
        "session": semantic,
        "claims": {
            "portable_session_integrity_verified": True,
            "trust_registry_verified": True,
            "trusted_keys_active_at_review": True,
            "trusted_keys_not_revoked_at_review": True,
            "source_operator_key_active_at_attestation": True,
            "source_inventory_signature_verified": True,
            "operator_key_possession_verified": True,
            "operator_human_identity_verified": False,
            "operator_reviewer_separation_verified": True,
            "review_signature_verified": True,
            "reviewer_key_possession_verified": True,
            "reviewer_human_identity_verified": False,
            "target_environment_reverified": False,
            "canonical_e7_sources_recomputed": False,
            "hardware_flight_acceptance": "not_claimed",
            "physical_release_enabled": False,
            "flight_control_enabled": False,
            "payload_release_requested": False,
        },
        "gate_passed": True,
        "reasons": [],
    }
    signed_payload = _signed_payload_bytes(document)
    document["signature"] = {
        "algorithm": SIGNATURE_ALGORITHM,
        "signed_payload_sha256": hashlib.sha256(signed_payload).hexdigest(),
        "signature_ed25519_hex": signing_key.sign(signed_payload).signature.hex(),
    }
    document["canonical_hash"] = canonical_sha256(document)
    _atomic_write(review_path, document)
    verify_gr01_release_session_review_receipt(
        review_path,
        trust_registry=trust_registry,
        expected_trust_registry_sha256=expected_trust_registry_sha256,
    )
    return document


def verify_gr01_release_session_review_receipt(
    receipt: str | Path,
    *,
    trust_registry: str | Path,
    expected_trust_registry_sha256: str,
) -> dict[str, Any]:
    path = _regular_file(receipt, name="GR01 portable review receipt")
    document = strict_object(
        strict_json_loads(path.read_bytes(), name="GR01 portable review receipt"),
        name="Gr01ReleaseSessionReview",
        required=frozenset(
            {
                "event",
                "schema_version",
                "reviewed_at_utc",
                "bundle_id",
                "reviewer",
                "trust_registry",
                "source_attestation",
                "transfer",
                "session",
                "claims",
                "gate_passed",
                "reasons",
                "signature",
                "canonical_hash",
            }
        ),
    )
    if document["event"] != REVIEW_EVENT or document["schema_version"] != REVIEW_SCHEMA_VERSION:
        raise Gr01ReleaseReviewError("GR01 portable review schema identity is invalid")
    reviewed_at = require_text(document["reviewed_at_utc"], "reviewed_at_utc")
    try:
        parsed_reviewed_at = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Gr01ReleaseReviewError("reviewed_at_utc is not an ISO-8601 timestamp") from exc
    if parsed_reviewed_at.tzinfo is None or parsed_reviewed_at.utcoffset() is None:
        raise Gr01ReleaseReviewError("reviewed_at_utc must include a timezone")
    bundle_id = require_text(document["bundle_id"], "bundle_id")
    try:
        if str(UUID(bundle_id)) != bundle_id:
            raise ValueError
    except ValueError as exc:
        raise Gr01ReleaseReviewError("bundle_id must be a canonical UUID") from exc
    reviewer = strict_object(
        document["reviewer"],
        name="reviewer",
        required=frozenset({"identity", "key_id", "public_key_ed25519_hex"}),
    )
    require_text(reviewer["identity"], "reviewer.identity")
    require_text(reviewer["key_id"], "reviewer.key_id")
    public_key_hex = _require_hex_32(
        reviewer["public_key_ed25519_hex"],
        "reviewer.public_key_ed25519_hex",
    )
    verified_registry = verify_gr01_review_trust_registry(
        trust_registry,
        expected_file_sha256=expected_trust_registry_sha256,
        at_utc=parsed_reviewed_at,
    )
    trusted_reviewer = require_trusted_gr01_principal(
        verified_registry,
        role="reviewer",
        key_id=reviewer["key_id"],
        identity=reviewer["identity"],
        public_key_ed25519_hex=public_key_hex,
        at_utc=parsed_reviewed_at,
    )
    registry_projection = strict_object(
        document["trust_registry"],
        name="trust_registry",
        required=frozenset(
            {
                "registry_id",
                "file_bytes",
                "file_sha256",
                "expected_file_sha256",
                "canonical_hash",
                "operator_principal_id",
                "reviewer_principal_id",
                "active_at_review",
                "revocation_checked",
            }
        ),
    )
    registry_id = require_text(registry_projection["registry_id"], "trust_registry.registry_id")
    try:
        if str(UUID(registry_id)) != registry_id:
            raise ValueError
    except ValueError as exc:
        raise Gr01ReleaseReviewError("trust_registry.registry_id must be a canonical UUID") from exc
    require_int(registry_projection["file_bytes"], "trust_registry.file_bytes", minimum=1)
    for name in ("file_sha256", "expected_file_sha256", "canonical_hash"):
        require_sha256(registry_projection[name], f"trust_registry.{name}")
    require_text(
        registry_projection["operator_principal_id"],
        "trust_registry.operator_principal_id",
    )
    require_text(
        registry_projection["reviewer_principal_id"],
        "trust_registry.reviewer_principal_id",
    )
    require_bool(registry_projection["active_at_review"], "trust_registry.active_at_review")
    require_bool(registry_projection["revocation_checked"], "trust_registry.revocation_checked")
    if (
        registry_projection["registry_id"] != verified_registry["document"]["registry_id"]
        or registry_projection["file_bytes"] != verified_registry["file_bytes"]
        or registry_projection["file_sha256"] != verified_registry["file_sha256"]
        or registry_projection["expected_file_sha256"] != verified_registry["file_sha256"]
        or registry_projection["canonical_hash"] != verified_registry["document"]["canonical_hash"]
        or registry_projection["reviewer_principal_id"] != trusted_reviewer["principal_id"]
        or registry_projection["active_at_review"] is not True
        or registry_projection["revocation_checked"] is not True
    ):
        raise Gr01ReleaseReviewError("portable review trust registry binding is invalid")
    source_attestation = strict_object(
        document["source_attestation"],
        name="source_attestation",
        required=frozenset(
            {
                "attested_at_utc",
                "file_bytes",
                "file_sha256",
                "canonical_hash",
                "operator_identity",
                "operator_key_id",
                "operator_public_key_ed25519_hex",
                "inventory_signature_verified",
            }
        ),
    )
    source_attested_at_text = require_text(
        source_attestation["attested_at_utc"],
        "source_attestation.attested_at_utc",
    )
    try:
        source_attested_at = datetime.fromisoformat(source_attested_at_text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Gr01ReleaseReviewError(
            "source_attestation.attested_at_utc is not an ISO-8601 timestamp"
        ) from exc
    if source_attested_at.tzinfo is None or source_attested_at.utcoffset() is None:
        raise Gr01ReleaseReviewError("source_attestation.attested_at_utc must include a timezone")
    if source_attested_at > parsed_reviewed_at:
        raise Gr01ReleaseReviewError("source attestation cannot postdate the portable review")
    require_int(source_attestation["file_bytes"], "source_attestation.file_bytes", minimum=1)
    for name in ("file_sha256", "canonical_hash"):
        require_sha256(source_attestation[name], f"source_attestation.{name}")
    operator_identity = require_text(
        source_attestation["operator_identity"], "source_attestation.operator_identity"
    )
    operator_key_id = require_text(
        source_attestation["operator_key_id"], "source_attestation.operator_key_id"
    )
    operator_public_key = _require_hex_32(
        source_attestation["operator_public_key_ed25519_hex"],
        "source_attestation.operator_public_key_ed25519_hex",
    )
    if (
        require_bool(
            source_attestation["inventory_signature_verified"],
            "source_attestation.inventory_signature_verified",
        )
        is not True
    ):
        raise Gr01ReleaseReviewError("portable review source signature claim must be true")
    if (
        operator_identity == reviewer["identity"]
        or operator_key_id == reviewer["key_id"]
        or operator_public_key == public_key_hex
    ):
        raise Gr01ReleaseReviewError("portable review operator and reviewer separation is invalid")
    source_time_registry = verify_gr01_review_trust_registry(
        trust_registry,
        expected_file_sha256=expected_trust_registry_sha256,
        at_utc=source_attested_at,
    )
    trusted_operator = require_trusted_gr01_principal(
        source_time_registry,
        role="source_operator",
        key_id=operator_key_id,
        identity=operator_identity,
        public_key_ed25519_hex=operator_public_key,
        at_utc=source_attested_at,
    )
    require_trusted_gr01_principal(
        verified_registry,
        role="source_operator",
        key_id=operator_key_id,
        identity=operator_identity,
        public_key_ed25519_hex=operator_public_key,
        at_utc=parsed_reviewed_at,
    )
    if registry_projection["operator_principal_id"] != trusted_operator["principal_id"]:
        raise Gr01ReleaseReviewError("portable review operator registry binding is invalid")
    transfer = strict_object(
        document["transfer"],
        name="transfer",
        required=frozenset(
            {
                "inventory_file_bytes",
                "inventory_file_sha256",
                "expected_inventory_file_sha256",
                "inventory_hash_matches_expected",
                "inventory_canonical_hash",
                "transfer_receipt_file_bytes",
                "transfer_receipt_file_sha256",
                "transfer_receipt_canonical_hash",
                "bundle_content_hash",
                "byte_exact_copy",
            }
        ),
    )
    for name in ("inventory_file_bytes", "transfer_receipt_file_bytes"):
        require_int(transfer[name], f"transfer.{name}", minimum=1)
    for name in (
        "expected_inventory_file_sha256",
        "inventory_file_sha256",
        "inventory_canonical_hash",
        "transfer_receipt_file_sha256",
        "transfer_receipt_canonical_hash",
        "bundle_content_hash",
    ):
        require_sha256(transfer[name], f"transfer.{name}")
    if (
        transfer["expected_inventory_file_sha256"] != transfer["inventory_file_sha256"]
        or require_bool(
            transfer["inventory_hash_matches_expected"],
            "transfer.inventory_hash_matches_expected",
        )
        is not True
    ):
        raise Gr01ReleaseReviewError("portable review inventory trust anchor does not match")
    if require_bool(transfer["byte_exact_copy"], "transfer.byte_exact_copy") is not True:
        raise Gr01ReleaseReviewError("portable review requires a byte-exact copy")
    session = strict_object(
        document["session"],
        name="session",
        required=frozenset(
            {
                "record_path",
                "record_bytes",
                "record_sha256",
                "session_id",
                "session_schema_version",
                "source_count",
                "five_source_hashes_verified",
                "portable_handoff_verified",
                "bench_recomputed",
                "configuration_matches_plan_bench_and_e7",
                "release_manifest_gr01_binding_matched",
                "stored_release_gate_passed",
                "stored_release_allowed",
                "stored_release_blocker_count",
            }
        ),
    )
    if session["record_path"] != RECORD_NAME:
        raise Gr01ReleaseReviewError("portable review record path is invalid")
    require_int(session["record_bytes"], "session.record_bytes", minimum=1)
    require_sha256(session["record_sha256"], "session.record_sha256")
    require_text(session["session_id"], "session.session_id")
    if session["session_schema_version"] != SESSION_SCHEMA_VERSION or session["source_count"] != 5:
        raise Gr01ReleaseReviewError("portable review session identity is invalid")
    for name in (
        "five_source_hashes_verified",
        "portable_handoff_verified",
        "bench_recomputed",
        "configuration_matches_plan_bench_and_e7",
        "release_manifest_gr01_binding_matched",
    ):
        if require_bool(session[name], f"session.{name}") is not True:
            raise Gr01ReleaseReviewError(f"session.{name} must be true")
    for name in ("stored_release_gate_passed", "stored_release_allowed"):
        require_bool(session[name], f"session.{name}")
    require_int(
        session["stored_release_blocker_count"],
        "session.stored_release_blocker_count",
        minimum=0,
    )
    claims = strict_object(
        document["claims"],
        name="claims",
        required=frozenset(
            {
                "portable_session_integrity_verified",
                "trust_registry_verified",
                "trusted_keys_active_at_review",
                "trusted_keys_not_revoked_at_review",
                "source_operator_key_active_at_attestation",
                "source_inventory_signature_verified",
                "operator_key_possession_verified",
                "operator_human_identity_verified",
                "operator_reviewer_separation_verified",
                "review_signature_verified",
                "reviewer_key_possession_verified",
                "reviewer_human_identity_verified",
                "target_environment_reverified",
                "canonical_e7_sources_recomputed",
                "hardware_flight_acceptance",
                "physical_release_enabled",
                "flight_control_enabled",
                "payload_release_requested",
            }
        ),
    )
    if (
        claims["portable_session_integrity_verified"] is not True
        or claims["trust_registry_verified"] is not True
        or claims["trusted_keys_active_at_review"] is not True
        or claims["trusted_keys_not_revoked_at_review"] is not True
        or claims["source_operator_key_active_at_attestation"] is not True
        or claims["source_inventory_signature_verified"] is not True
        or claims["operator_key_possession_verified"] is not True
        or claims["operator_human_identity_verified"] is not False
        or claims["operator_reviewer_separation_verified"] is not True
        or claims["review_signature_verified"] is not True
        or claims["reviewer_key_possession_verified"] is not True
        or claims["reviewer_human_identity_verified"] is not False
        or claims["target_environment_reverified"] is not False
        or claims["canonical_e7_sources_recomputed"] is not False
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
        raise Gr01ReleaseReviewError("portable review claim boundary is invalid")
    if document["gate_passed"] is not True or document["reasons"] != []:
        raise Gr01ReleaseReviewError("portable review stored gate is not a clean pass")
    signature = strict_object(
        document["signature"],
        name="signature",
        required=frozenset({"algorithm", "signed_payload_sha256", "signature_ed25519_hex"}),
    )
    if signature["algorithm"] != SIGNATURE_ALGORITHM:
        raise Gr01ReleaseReviewError("portable review signature algorithm is invalid")
    signed_payload_sha256 = require_sha256(
        signature["signed_payload_sha256"],
        "signature.signed_payload_sha256",
    )
    signature_hex = require_text(
        signature["signature_ed25519_hex"],
        "signature.signature_ed25519_hex",
    ).lower()
    try:
        signature_bytes = bytes.fromhex(signature_hex)
    except ValueError as exc:
        raise Gr01ReleaseReviewError("portable review signature is not hexadecimal") from exc
    if len(signature_hex) != 128 or len(signature_bytes) != 64:
        raise Gr01ReleaseReviewError("portable review signature must contain 64 bytes")
    signed_document = dict(document)
    signed_document.pop("signature")
    signed_document.pop("canonical_hash")
    signed_payload = _signed_payload_bytes(signed_document)
    if signed_payload_sha256 != hashlib.sha256(signed_payload).hexdigest():
        raise Gr01ReleaseReviewError("portable review signed payload hash does not match")
    _, verify_key_type, bad_signature_error = _signing_types()
    try:
        verify_key_type(bytes.fromhex(trusted_reviewer["public_key_ed25519_hex"])).verify(
            signed_payload,
            signature_bytes,
        )
    except bad_signature_error as exc:
        raise Gr01ReleaseReviewError("portable review Ed25519 signature is invalid") from exc
    claimed_hash = require_sha256(document["canonical_hash"], "canonical_hash")
    unhashed = dict(document)
    unhashed.pop("canonical_hash")
    if claimed_hash != canonical_sha256(unhashed):
        raise Gr01ReleaseReviewError("portable review canonical hash does not match")
    return document


def _atomic_write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
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
    snapshot = subparsers.add_parser("snapshot", help="verify and inventory a field session")
    snapshot.add_argument("--session-root", required=True, type=Path)
    snapshot.add_argument("--inventory-out", required=True, type=Path)
    snapshot.add_argument("--version-manifest", type=Path)
    snapshot.add_argument("--bundle-id")
    attest = subparsers.add_parser("attest-source", help="sign a field inventory at source")
    attest.add_argument("--inventory", required=True, type=Path)
    attest.add_argument("--operator", required=True)
    attest.add_argument("--operator-key-id", required=True)
    attest.add_argument("--operator-signing-key-env", required=True)
    attest.add_argument("--out", required=True, type=Path)
    review = subparsers.add_parser("review", help="review a copied session on another machine")
    review.add_argument("--inventory", required=True, type=Path)
    review.add_argument("--expected-inventory-sha256", required=True)
    review.add_argument("--source-attestation", required=True, type=Path)
    review.add_argument("--trust-registry", required=True, type=Path)
    review.add_argument("--expected-trust-registry-sha256", required=True)
    review.add_argument("--reviewer", required=True)
    review.add_argument("--reviewer-key-id", required=True)
    review.add_argument("--reviewer-signing-key-env", required=True)
    review.add_argument("--destination-root", required=True, type=Path)
    review.add_argument("--transfer-receipt-out", required=True, type=Path)
    review.add_argument("--out", required=True, type=Path)
    verify = subparsers.add_parser("verify", help="verify a saved portable review receipt")
    verify.add_argument("receipt", type=Path)
    verify.add_argument("--trust-registry", required=True, type=Path)
    verify.add_argument("--expected-trust-registry-sha256", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "snapshot":
            document = snapshot_gr01_release_session_bundle(
                args.session_root,
                args.inventory_out,
                version_manifest_path=args.version_manifest,
                bundle_id=args.bundle_id,
            )
        elif args.command == "attest-source":
            document = create_gr01_release_session_source_attestation(
                args.inventory,
                args.out,
                operator=args.operator,
                operator_key_id=args.operator_key_id,
                operator_signing_key_environment=args.operator_signing_key_env,
            )
        elif args.command == "review":
            document = review_gr01_release_session_bundle(
                args.inventory,
                args.source_attestation,
                args.destination_root,
                args.transfer_receipt_out,
                args.out,
                expected_inventory_sha256=args.expected_inventory_sha256,
                trust_registry=args.trust_registry,
                expected_trust_registry_sha256=args.expected_trust_registry_sha256,
                reviewer=args.reviewer,
                reviewer_key_id=args.reviewer_key_id,
                reviewer_signing_key_environment=args.reviewer_signing_key_env,
            )
        else:
            document = verify_gr01_release_session_review_receipt(
                args.receipt,
                trust_registry=args.trust_registry,
                expected_trust_registry_sha256=args.expected_trust_registry_sha256,
            )
    except (AutonomyContractError, Gr01ReleaseReviewError, OSError, TypeError, ValueError) as exc:
        print(f"GR01 release-session review: {exc}")
        return 2
    print(json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
