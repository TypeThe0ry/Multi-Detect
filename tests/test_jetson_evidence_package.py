from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

import multidetect
from multidetect.jetson_evidence_package import verify_jetson_evidence_package

ROOT = Path(__file__).resolve().parents[1]


def _package(
    tmp_path: Path,
    *,
    extra_member: tarfile.TarInfo | None = None,
    extra_payload: bytes = b"",
) -> tuple[Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    files = {
        "configs/product-version.json": b'{"release_label":"0.3.0-pre.8"}\n',
        "scripts/record_jetson_rtsp_evidence.sh": b"#!/usr/bin/env bash\n",
        "src/multidetect/__init__.py": b'"""package"""\n',
    }
    manifest = "".join(
        f"{hashlib.sha256(payload).hexdigest()}  {path}\n"
        for path, payload in sorted(files.items(), key=lambda item: item[0].casefold())
    ).encode("ascii")
    archive_path = tmp_path / "tools.tar"
    with tarfile.open(archive_path, mode="w") as archive:
        for path, payload in {**files, "source-sha256.txt": manifest}.items():
            member = tarfile.TarInfo(path)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        if extra_member is not None:
            extra_member.size = len(extra_payload)
            archive.addfile(extra_member, io.BytesIO(extra_payload))
    receipt = {
        "schema_version": 1,
        "event": "jetson_evidence_capture_package_created",
        "deployment_id": "v126-test",
        "created_at_utc": "2026-09-06T08:00:00Z",
        "source_git_head": "a" * 40,
        "source_dirty": False,
        "archive_name": archive_path.name,
        "archive_bytes": archive_path.stat().st_size,
        "archive_sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
        "file_count": len(files),
        "source_manifest_sha256": hashlib.sha256(manifest).hexdigest(),
        "package_self_verified": True,
        "verified_file_count": len(files),
        "remote_tools_root": "/home/jetson/Multi-Detect-evidence-tools",
        "runtime_python": "/home/jetson/Multi-Detect/.venv/bin/python",
        "package_only": True,
        "service_restart_requested": False,
        "sudo_used": False,
        "release_ready": False,
    }
    receipt_path = tmp_path / "tools.package.json"
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return receipt_path, archive_path


def test_package_verifier_reopens_closed_tar_and_all_hashes(tmp_path: Path) -> None:
    receipt_path, archive_path = _package(tmp_path)
    result = verify_jetson_evidence_package(receipt_path)

    assert result["passed"] is True
    assert result["source_dirty"] is False
    assert result["verified_source_file_count"] == 3
    assert result["archive_file_sha256"] == hashlib.sha256(archive_path.read_bytes()).hexdigest()
    assert result["path_safety_verified"] is True
    assert result["closed_inventory_verified"] is True
    assert result["all_source_hashes_verified"] is True
    assert result["remote_install_verified"] is False
    assert result["hardware_acceptance"] is False
    assert result["release_ready"] is False

    schema = json.loads(
        (ROOT / "configs/schemas/jetson-evidence-tools-package.schema.json").read_text(
            encoding="utf-8"
        )
    )
    document = json.loads(receipt_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)


def test_package_verifier_rejects_archive_and_receipt_tampering(tmp_path: Path) -> None:
    receipt_path, archive_path = _package(tmp_path)
    archive_path.write_bytes(archive_path.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="archive byte count changed"):
        verify_jetson_evidence_package(receipt_path)

    receipt_path, _ = _package(tmp_path / "claims")
    document = json.loads(receipt_path.read_text(encoding="utf-8"))
    document["release_ready"] = True
    receipt_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="release_ready must remain false"):
        verify_jetson_evidence_package(receipt_path)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "extra", "traversal"])
def test_package_verifier_rejects_links_extra_files_and_traversal(
    tmp_path: Path, kind: str
) -> None:
    if kind == "symlink":
        member = tarfile.TarInfo("video-link")
        member.type = tarfile.SYMTYPE
        member.linkname = "capture.mkv"
    elif kind == "hardlink":
        member = tarfile.TarInfo("video-link")
        member.type = tarfile.LNKTYPE
        member.linkname = "capture.mkv"
    elif kind == "extra":
        member = tarfile.TarInfo("extra.txt")
    else:
        member = tarfile.TarInfo("../escape.txt")
    receipt_path, _ = _package(tmp_path, extra_member=member, extra_payload=b"x")
    pattern = "unsafe path" if kind == "traversal" else "link or special|inventory differs"
    with pytest.raises(ValueError, match=pattern):
        verify_jetson_evidence_package(receipt_path)


def test_package_verifier_cli_and_lazy_export(tmp_path: Path) -> None:
    receipt_path, _ = _package(tmp_path)
    assert multidetect.verify_jetson_evidence_package is verify_jetson_evidence_package
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/verify_jetson_evidence_capture_package.py"),
            str(receipt_path),
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["passed"] is True
