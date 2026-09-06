from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

import multidetect
from multidetect.evidence_bundle_transfer import (
    create_evidence_transfer_inventory,
    verify_evidence_bundle_transfer,
    verify_evidence_bundle_transfer_receipt,
    verify_evidence_transfer_inventory,
)

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 6, 7, 0, tzinfo=timezone.utc)


def _source_bundle(tmp_path: Path) -> Path:
    root = tmp_path / "jetson-evidence"
    (root / "raw-replay" / "frames").mkdir(parents=True)
    (root / "capture.mkv").write_bytes(b"matroska-video")
    (root / "capture.manifest.json").write_text('{"schema_version":3}\n', encoding="utf-8")
    (root / "raw-replay" / "frames" / "000001.jpg").write_bytes(b"jpeg-frame")
    return root


def test_snapshot_copy_and_destination_verification_are_byte_exact(tmp_path: Path) -> None:
    source = _source_bundle(tmp_path)
    inventory_path = source / "transfer-inventory.json"
    inventory = create_evidence_transfer_inventory(
        source,
        inventory_path,
        bundle_id="12345678-1234-5678-9234-567812345678",
        utc_now=lambda: NOW,
    )

    assert inventory["inventory_path"] == "transfer-inventory.json"
    assert inventory["file_count"] == 3
    assert inventory["claims"] == {"hardware_acceptance": False, "release_ready": False}
    assert [item["path"] for item in inventory["files"]] == [
        "capture.manifest.json",
        "capture.mkv",
        "raw-replay/frames/000001.jpg",
    ]
    verified_inventory = verify_evidence_transfer_inventory(inventory_path)
    assert verified_inventory["document"] == inventory

    destination = tmp_path / "windows-evidence-renamed"
    shutil.copytree(source, destination)
    receipt_path = tmp_path / "transfer-verification.json"
    receipt = verify_evidence_bundle_transfer(
        destination / "transfer-inventory.json",
        destination,
        receipt_path,
        utc_now=lambda: NOW,
    )

    assert receipt["byte_exact_copy"] is True
    assert receipt["destination_root_name"] == "windows-evidence-renamed"
    assert receipt["source_bundle_content_hash"] == inventory["bundle_content_hash"]
    assert receipt["destination_bundle_content_hash"] == inventory["bundle_content_hash"]
    assert verify_evidence_bundle_transfer_receipt(receipt_path) == receipt

    inventory_schema = json.loads(
        (ROOT / "configs/schemas/evidence-bundle-transfer-inventory.schema.json").read_text(
            encoding="utf-8"
        )
    )
    receipt_schema = json.loads(
        (ROOT / "configs/schemas/evidence-bundle-transfer-verification.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator.check_schema(inventory_schema)
    Draft202012Validator.check_schema(receipt_schema)
    checker = FormatChecker()
    Draft202012Validator(inventory_schema, format_checker=checker).validate(inventory)
    Draft202012Validator(receipt_schema, format_checker=checker).validate(receipt)


@pytest.mark.parametrize("mutation", ["changed", "missing", "unexpected"])
def test_destination_mutations_fail_closed(tmp_path: Path, mutation: str) -> None:
    source = _source_bundle(tmp_path)
    inventory_path = source / "transfer-inventory.json"
    create_evidence_transfer_inventory(source, inventory_path, utc_now=lambda: NOW)
    destination = tmp_path / "destination"
    shutil.copytree(source, destination)
    if mutation == "changed":
        (destination / "capture.mkv").write_bytes(b"changed")
    elif mutation == "missing":
        (destination / "capture.mkv").unlink()
    else:
        (destination / "extra.txt").write_text("extra", encoding="utf-8")

    with pytest.raises(ValueError, match=f"{mutation}="):
        verify_evidence_bundle_transfer(
            destination / "transfer-inventory.json",
            destination,
            tmp_path / "receipt.json",
        )


def test_inventory_and_receipt_tampering_fail_closed(tmp_path: Path) -> None:
    source = _source_bundle(tmp_path)
    inventory_path = source / "transfer-inventory.json"
    create_evidence_transfer_inventory(source, inventory_path, utc_now=lambda: NOW)
    changed = json.loads(inventory_path.read_text(encoding="utf-8"))
    changed["claims"]["release_ready"] = True
    inventory_path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="release_ready claim must remain false"):
        verify_evidence_transfer_inventory(inventory_path)

    source = _source_bundle(tmp_path / "second")
    inventory_path = source / "transfer-inventory.json"
    create_evidence_transfer_inventory(source, inventory_path, utc_now=lambda: NOW)
    receipt_path = tmp_path / "receipt.json"
    verify_evidence_bundle_transfer(inventory_path, source, receipt_path, utc_now=lambda: NOW)
    changed = json.loads(receipt_path.read_text(encoding="utf-8"))
    changed["byte_exact_copy"] = False
    receipt_path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="byte_exact_copy"):
        verify_evidence_bundle_transfer_receipt(receipt_path)


def test_symlink_and_nested_receipt_are_rejected(tmp_path: Path) -> None:
    source = _source_bundle(tmp_path)
    inventory_path = source / "transfer-inventory.json"
    create_evidence_transfer_inventory(source, inventory_path, utc_now=lambda: NOW)
    with pytest.raises(ValueError, match="outside destination root"):
        verify_evidence_bundle_transfer(
            inventory_path,
            source,
            source / "transfer-verification.json",
        )

    link = source / "video-link.mkv"
    try:
        link.symlink_to(source / "capture.mkv")
    except OSError:
        pytest.skip("symlink creation is unavailable")
    inventory_path.unlink()
    with pytest.raises(ValueError, match="contains symlink"):
        create_evidence_transfer_inventory(source, inventory_path, utc_now=lambda: NOW)


def test_cli_help_and_lazy_exports() -> None:
    assert multidetect.create_evidence_transfer_inventory is create_evidence_transfer_inventory
    assert multidetect.verify_evidence_bundle_transfer is verify_evidence_bundle_transfer
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts/evidence_bundle_transfer.py"), "--help"],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "snapshot" in completed.stdout
    assert "verify-receipt" in completed.stdout
