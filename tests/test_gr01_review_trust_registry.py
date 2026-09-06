from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from multidetect.gr01_review_trust_registry import (
    create_gr01_review_trust_registry,
    require_trusted_gr01_principal,
    verify_gr01_review_trust_registry,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "configs" / "schemas" / "gr01-review-trust-registry.schema.json"
AT = datetime(2026, 9, 7, tzinfo=timezone.utc)


def _principal(
    role: str,
    suffix: str,
    public_key: str,
    *,
    expires_at: str = "2030-01-01T00:00:00Z",
    revoked: bool = False,
) -> dict[str, object]:
    return {
        "principal_id": f"principal-{suffix}",
        "identity": f"identity-{suffix}",
        "role": role,
        "key_id": f"key-{suffix}",
        "public_key_ed25519_hex": public_key,
        "valid_from_utc": "2026-01-01T00:00:00Z",
        "expires_at_utc": expires_at,
        "revoked": revoked,
        "revoked_at_utc": "2026-06-01T00:00:00Z" if revoked else None,
        "revocation_reason": "rotation" if revoked else None,
    }


def _registry(
    tmp_path: Path,
    *,
    operator: dict[str, object] | None = None,
    reviewer: dict[str, object] | None = None,
    expires_at: datetime = datetime(2030, 1, 1, tzinfo=timezone.utc),
) -> tuple[Path, dict[str, object]]:
    path = tmp_path / "trust-registry.json"
    document = create_gr01_review_trust_registry(
        path,
        registry_id="12345678-1234-5678-9234-567812345678",
        issued_at_utc=datetime(2025, 12, 31, tzinfo=timezone.utc),
        valid_from_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
        expires_at_utc=expires_at,
        principals=[
            operator or _principal("source_operator", "operator", "11" * 32),
            reviewer or _principal("reviewer", "reviewer", "22" * 32),
        ],
    )
    return path, document


def test_registry_schema_hash_anchor_and_active_principal_selection(tmp_path: Path) -> None:
    path, document = _registry(tmp_path)
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(
        schema,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    ).validate(document)
    expected_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    verified = verify_gr01_review_trust_registry(
        path,
        expected_file_sha256=expected_hash,
        at_utc=AT,
    )
    principal = require_trusted_gr01_principal(
        verified,
        role="source_operator",
        key_id="key-operator",
        identity="identity-operator",
        public_key_ed25519_hex="11" * 32,
        at_utc=AT,
    )
    assert principal["principal_id"] == "principal-operator"
    assert document["claims"]["human_identity_verified"] is False


def test_registry_rejects_wrong_external_hash(tmp_path: Path) -> None:
    path, _ = _registry(tmp_path)
    with pytest.raises(ValueError, match="SHA-256 is not trusted"):
        verify_gr01_review_trust_registry(
            path,
            expected_file_sha256="00" * 32,
            at_utc=AT,
        )


@pytest.mark.parametrize(
    ("operator", "message"),
    [
        (_principal("source_operator", "operator", "11" * 32, revoked=True), "revoked"),
        (
            _principal(
                "source_operator",
                "operator",
                "11" * 32,
                expires_at="2026-08-01T00:00:00Z",
            ),
            "expired or not active",
        ),
    ],
)
def test_registry_rejects_revoked_or_expired_selected_key(
    tmp_path: Path,
    operator: dict[str, object],
    message: str,
) -> None:
    path, _ = _registry(tmp_path, operator=operator)
    verified = verify_gr01_review_trust_registry(
        path,
        expected_file_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        at_utc=AT,
    )
    with pytest.raises(ValueError, match=message):
        require_trusted_gr01_principal(
            verified,
            role="source_operator",
            key_id="key-operator",
            identity="identity-operator",
            public_key_ed25519_hex="11" * 32,
            at_utc=AT,
        )


def test_registry_rejects_duplicate_key_before_output(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.json"
    with pytest.raises(ValueError, match="public_key_ed25519_hex values must be unique"):
        create_gr01_review_trust_registry(
            path,
            registry_id="12345678-1234-5678-9234-567812345678",
            issued_at_utc=datetime(2025, 12, 31, tzinfo=timezone.utc),
            valid_from_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
            expires_at_utc=datetime(2030, 1, 1, tzinfo=timezone.utc),
            principals=[
                _principal("source_operator", "operator", "11" * 32),
                _principal("reviewer", "reviewer", "11" * 32),
            ],
        )
    assert not path.exists()


def test_registry_cli_rejects_expired_registry(tmp_path: Path) -> None:
    path, _ = _registry(
        tmp_path,
        expires_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "verify_gr01_review_trust_registry.py"),
            "verify",
            str(path),
            "--expected-file-sha256",
            hashlib.sha256(path.read_bytes()).hexdigest(),
            "--at-utc",
            "2026-09-07T00:00:00Z",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert "not active at review time" in result.stdout


def test_registry_cli_creates_write_new_closed_document(tmp_path: Path) -> None:
    principals = tmp_path / "principals.json"
    principals.write_text(
        json.dumps(
            [
                _principal("source_operator", "operator", "11" * 32),
                _principal("reviewer", "reviewer", "22" * 32),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "created.json"
    command = [
        sys.executable,
        str(ROOT / "scripts" / "verify_gr01_review_trust_registry.py"),
        "create",
        "--principals-json",
        str(principals),
        "--registry-id",
        "12345678-1234-5678-9234-567812345678",
        "--issued-at-utc",
        "2025-12-31T00:00:00Z",
        "--valid-from-utc",
        "2026-01-01T00:00:00Z",
        "--expires-at-utc",
        "2030-01-01T00:00:00Z",
        "--out",
        str(output),
    ]
    first = subprocess.run(command, cwd=ROOT, check=False, capture_output=True, text=True)
    second = subprocess.run(command, cwd=ROOT, check=False, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr or first.stdout
    assert second.returncode == 2
    assert "output already exists" in second.stdout
    Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8"))).validate(
        json.loads(output.read_text(encoding="utf-8"))
    )
