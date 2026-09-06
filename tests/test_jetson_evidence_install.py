from __future__ import annotations

import base64
import hashlib
import hmac
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

import multidetect
import multidetect.jetson_evidence_install as install_module
from multidetect.jetson_evidence_install import verify_jetson_evidence_installation

ROOT = Path(__file__).resolve().parents[1]
SOURCE_HEAD = "a" * 40
ARCHIVE_SHA256 = "b" * 64
REMOTE_ROOT = "/home/jetson/Multi-Detect-evidence-tools"
RUNTIME_PYTHON = "/home/jetson/Multi-Detect/.venv/bin/python"
DEPLOYMENT_ID = "v127-test"
REMOTE_TARGET = f"{REMOTE_ROOT}/{DEPLOYMENT_ID}"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _package_result(*, package_only: bool = False) -> dict[str, object]:
    return {
        "package_only": package_only,
        "deployment_id": DEPLOYMENT_ID,
        "source_git_head": SOURCE_HEAD,
        "archive_file_sha256": ARCHIVE_SHA256,
        "remote_tools_root": REMOTE_ROOT,
        "runtime_python": RUNTIME_PYTHON,
    }


def _installation(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    package_receipt = tmp_path / "tools.package.json"
    package_receipt.write_text('{"fixture":"package receipt"}\n', encoding="utf-8")
    remote_output = tmp_path / "tools.remote-output.txt"
    remote_output.write_text(
        "manifest verified\n"
        f"JETSON_EVIDENCE_TOOLS_READY={REMOTE_TARGET}\n"
        "JETSON_EVIDENCE_RUN=MULTIDETECT_ROOT=/installed "
        "MULTIDETECT_PYTHON=/venv/python bash /installed/record.sh\n",
        encoding="utf-8",
    )
    host_key_bytes = b"multidetect-v129-test-host-key"
    encoded_host_key = base64.b64encode(host_key_bytes).decode("ascii")
    host_key_snapshot = tmp_path / "tools.ssh-host-keys"
    host_key_snapshot.write_text(
        f"multidetect-jetson ssh-ed25519 {encoded_host_key}\n", encoding="ascii"
    )
    host_key_fingerprint = "SHA256:" + base64.b64encode(
        hashlib.sha256(host_key_bytes).digest()
    ).decode("ascii").rstrip("=")
    document: dict[str, object] = {
        "schema_version": 3,
        "event": "jetson_evidence_tools_installed",
        "installed_at_utc": "2026-09-06T09:00:00Z",
        "deployment_id": DEPLOYMENT_ID,
        "host_alias": "multidetect-jetson",
        "remote_user": "jetson",
        "ssh_port": 22,
        "resolved_host": "multidetect-jetson",
        "ssh_host_keys": {
            "file_name": host_key_snapshot.name,
            "file_bytes": host_key_snapshot.stat().st_size,
            "file_sha256": _sha256(host_key_snapshot),
        },
        "ssh_host_key_fingerprints": [host_key_fingerprint],
        "remote_target": REMOTE_TARGET,
        "runtime_python": RUNTIME_PYTHON,
        "source_git_head": SOURCE_HEAD,
        "package_receipt": {
            "file_name": package_receipt.name,
            "file_bytes": package_receipt.stat().st_size,
            "file_sha256": _sha256(package_receipt),
        },
        "archive_sha256": ARCHIVE_SHA256,
        "remote_output": {
            "file_name": remote_output.name,
            "file_bytes": remote_output.stat().st_size,
            "file_sha256": _sha256(remote_output),
            "ready_marker": f"JETSON_EVIDENCE_TOOLS_READY={REMOTE_TARGET}",
            "run_marker_present": True,
        },
        "remote_checks": {
            "package_manifest_verified": True,
            "python_compileall_passed": True,
            "bash_syntax_passed": True,
            "module_imports_passed": True,
            "recorder_executable": True,
            "atomic_install_completed": True,
        },
        "service_restart_requested": False,
        "sudo_used": False,
        "remote_install_verified": True,
        "hardware_acceptance": False,
        "release_ready": False,
    }
    receipt = tmp_path / "tools.installation.json"
    receipt.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return receipt, document


def _rewrite(receipt: Path, document: dict[str, object]) -> None:
    receipt.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def test_install_verifier_reopens_bound_artifacts_and_validates_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt, document = _installation(tmp_path)
    monkeypatch.setattr(
        install_module, "verify_jetson_evidence_package", lambda _: _package_result()
    )

    result = verify_jetson_evidence_installation(receipt)

    assert result["passed"] is True
    assert result["package_verified"] is True
    assert result["remote_markers_verified"] is True
    assert result["all_remote_checks_passed"] is True
    assert result["remote_install_verified"] is True
    assert result["hardware_acceptance"] is False
    assert result["release_ready"] is False

    schema = json.loads(
        (ROOT / "configs/schemas/jetson-evidence-tools-installation.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)


def test_install_verifier_rejects_package_only_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt, _ = _installation(tmp_path)
    monkeypatch.setattr(
        install_module,
        "verify_jetson_evidence_package",
        lambda _: _package_result(package_only=True),
    )
    with pytest.raises(ValueError, match="must not be package-only"):
        verify_jetson_evidence_installation(receipt)


def test_install_verifier_rejects_bound_remote_output_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt, _ = _installation(tmp_path)
    (tmp_path / "tools.remote-output.txt").write_text("changed\n", encoding="utf-8")
    monkeypatch.setattr(
        install_module, "verify_jetson_evidence_package", lambda _: _package_result()
    )
    with pytest.raises(ValueError, match="byte count changed|SHA-256 changed"):
        verify_jetson_evidence_installation(receipt)


@pytest.mark.parametrize(
    "failure", ["tamper", "wrong_host", "wrong_fingerprint", "duplicate_fingerprint", "revoked"]
)
def test_install_verifier_rejects_invalid_host_key_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    receipt, document = _installation(tmp_path)
    host_key_path = tmp_path / "tools.ssh-host-keys"
    if failure == "tamper":
        host_key_path.write_text("changed\n", encoding="ascii")
    elif failure == "wrong_host":
        host_key_path.write_text(
            host_key_path.read_text(encoding="ascii").replace(
                "multidetect-jetson", "different-host"
            ),
            encoding="ascii",
        )
    elif failure == "wrong_fingerprint":
        document["ssh_host_key_fingerprints"] = ["SHA256:" + "A" * 43]
    elif failure == "duplicate_fingerprint":
        fingerprints = document["ssh_host_key_fingerprints"]
        assert isinstance(fingerprints, list)
        document["ssh_host_key_fingerprints"] = fingerprints * 2
    else:
        host_key_path.write_text(
            "@revoked " + host_key_path.read_text(encoding="ascii"), encoding="ascii"
        )
    if failure in {"wrong_host", "revoked"}:
        host_key_ref = document["ssh_host_keys"]
        assert isinstance(host_key_ref, dict)
        host_key_ref["file_bytes"] = host_key_path.stat().st_size
        host_key_ref["file_sha256"] = _sha256(host_key_path)
    _rewrite(receipt, document)
    monkeypatch.setattr(
        install_module, "verify_jetson_evidence_package", lambda _: _package_result()
    )
    patterns = {
        "tamper": "byte count changed|SHA-256 changed",
        "wrong_host": "does not match resolved host",
        "wrong_fingerprint": "fingerprints differ",
        "duplicate_fingerprint": "fingerprints differ",
        "revoked": "revoked key",
    }
    with pytest.raises(ValueError, match=patterns[failure]):
        verify_jetson_evidence_installation(receipt)


def test_install_verifier_accepts_hashed_known_host_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt, document = _installation(tmp_path)
    host_key_path = tmp_path / "tools.ssh-host-keys"
    fields = host_key_path.read_text(encoding="ascii").split()
    salt = b"multidetect-v129-known-host-salt"
    digest = hmac.new(salt, b"multidetect-jetson", hashlib.sha1).digest()
    hashed_host = "|1|{}|{}".format(
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )
    host_key_path.write_text(f"{hashed_host} {fields[1]} {fields[2]}\n", encoding="ascii")
    host_key_ref = document["ssh_host_keys"]
    assert isinstance(host_key_ref, dict)
    host_key_ref["file_bytes"] = host_key_path.stat().st_size
    host_key_ref["file_sha256"] = _sha256(host_key_path)
    _rewrite(receipt, document)
    monkeypatch.setattr(
        install_module, "verify_jetson_evidence_package", lambda _: _package_result()
    )

    result = verify_jetson_evidence_installation(receipt)

    assert result["remote_install_verified"] is True
    assert result["ssh_host_keys_sha256"] == _sha256(host_key_path)


@pytest.mark.parametrize("failure", ["duplicate_ready", "missing_run", "failed_check"])
def test_install_verifier_rejects_invalid_remote_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    receipt, document = _installation(tmp_path)
    remote_path = tmp_path / "tools.remote-output.txt"
    if failure == "duplicate_ready":
        remote_path.write_text(
            remote_path.read_text(encoding="utf-8")
            + f"JETSON_EVIDENCE_TOOLS_READY={REMOTE_TARGET}\n",
            encoding="utf-8",
        )
    elif failure == "missing_run":
        remote_path.write_text(f"JETSON_EVIDENCE_TOOLS_READY={REMOTE_TARGET}\n", encoding="utf-8")
    else:
        checks = document["remote_checks"]
        assert isinstance(checks, dict)
        checks["bash_syntax_passed"] = False
    if failure != "failed_check":
        remote_ref = document["remote_output"]
        assert isinstance(remote_ref, dict)
        remote_ref["file_bytes"] = remote_path.stat().st_size
        remote_ref["file_sha256"] = _sha256(remote_path)
    _rewrite(receipt, document)
    monkeypatch.setattr(
        install_module, "verify_jetson_evidence_package", lambda _: _package_result()
    )
    pattern = "ready marker" if failure == "duplicate_ready" else "run marker|must pass"
    with pytest.raises(ValueError, match=pattern):
        verify_jetson_evidence_installation(receipt)


@pytest.mark.parametrize("field", ["release_ready", "hardware_acceptance", "sudo_used"])
def test_install_verifier_rejects_unsafe_positive_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    receipt, document = _installation(tmp_path)
    changed = deepcopy(document)
    changed[field] = True
    _rewrite(receipt, changed)
    monkeypatch.setattr(
        install_module, "verify_jetson_evidence_package", lambda _: _package_result()
    )
    with pytest.raises(ValueError, match=f"{field} must remain false"):
        verify_jetson_evidence_installation(receipt)


def test_install_verifier_rejects_package_identity_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt, _ = _installation(tmp_path)
    mismatched = _package_result()
    mismatched["source_git_head"] = "c" * 40
    monkeypatch.setattr(install_module, "verify_jetson_evidence_package", lambda _: mismatched)
    with pytest.raises(ValueError, match="source identity differ"):
        verify_jetson_evidence_installation(receipt)


@pytest.mark.parametrize(
    ("field", "value", "pattern"),
    [
        ("remote_user", "bad user", "remote user is invalid"),
        ("ssh_port", 0, "ssh_port must be at least 1"),
        ("ssh_port", 65536, "ssh_port must be at most 65535"),
    ],
)
def test_install_verifier_rejects_invalid_ssh_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
    pattern: str,
) -> None:
    receipt, document = _installation(tmp_path)
    document[field] = value
    _rewrite(receipt, document)
    monkeypatch.setattr(
        install_module, "verify_jetson_evidence_package", lambda _: _package_result()
    )
    with pytest.raises(ValueError, match=pattern):
        verify_jetson_evidence_installation(receipt)


def test_install_verifier_cli_help_and_lazy_export() -> None:
    assert multidetect.verify_jetson_evidence_installation is verify_jetson_evidence_installation
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/verify_jetson_evidence_installation.py"),
            "--help",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "installation receipt" in completed.stdout
