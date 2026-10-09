from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from multidetect.px4_runtime_capture_receipt import (
    CAPTURE_EVENT,
    CAPTURE_SCHEMA,
    EVENT,
    Px4RuntimeCaptureReceiptError,
    build_px4_runtime_capture_receipt,
    main,
    verify_main,
    verify_px4_runtime_capture_receipt,
)

ROOT = Path(__file__).resolve().parents[1]


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write(path: Path, payload: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    path.write_bytes(payload)
    return path


def _fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, Path]]:
    base = tmp_path / "capture"
    base.mkdir()
    files = {
        "mission": _write(base / "mission.json", b"mission-source\n"),
        "runtime_observation": _write(base / "live" / "observation.json", b"live-source\n"),
        "runtime_profile": _write(base / "profile.json", b"profile-source\n"),
        "mission_feedback": _write(base / "feedback" / "px4-mission-feedback.json", b"feedback\n"),
        "endpoint_vehicle_identity": _write(
            base / "identity" / "px4-endpoint-vehicle.json", b"identity\n"
        ),
        "join": _write(base / "join" / "live-runtime-observation-mission-binding.json", b"join\n"),
    }
    summary = {
        "schema": CAPTURE_SCHEMA,
        "schema_version": 1,
        "event": CAPTURE_EVENT,
        "runtime_mode": "hil",
        "mission": {
            "path": str(files["mission"]),
            "mission_id": "receipt-test",
            "mission_hash": "a" * 64,
        },
        "runtime_observation": {
            "path": str(files["runtime_observation"]),
            "result_sha256": "b" * 64,
            "runtime_descriptor_hash": "c" * 64,
            "runtime_input_hash": "d" * 64,
        },
        "mission_feedback": {
            "path": str(files["mission_feedback"]),
            "evidence_sha256": "e" * 64,
            "gate_passed": True,
        },
        "runtime_profile": {
            "path": str(files["runtime_profile"]),
            "document_sha256": "f" * 64,
        },
        "endpoint_vehicle_identity": {
            "path": str(files["endpoint_vehicle_identity"]),
            "identity_sha256": "1" * 64,
            "gate_passed": True,
        },
        "transport": None,
        "join": {
            "path": str(files["join"]),
            "evidence_sha256": "2" * 64,
            "gate_passed": True,
            "reasons": [],
        },
        "claims": {
            "release_eligible": False,
            "hardware_acceptance": False,
            "hil_acceptance": False,
            "real_acceptance": False,
            "operator_visual_acceptance": False,
        },
    }
    summary_path = _write(
        base / "capture-summary.json",
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return base, summary_path, files


def test_receipt_builds_as_deterministic_six_source_closure(tmp_path: Path) -> None:
    base, summary_path, files = _fixture(tmp_path)
    output = base / "capture-receipt.json"

    document = build_px4_runtime_capture_receipt(summary_path, base, output)
    assert document["schema"] == "multidetect.px4-runtime-capture-receipt"
    assert document["event"] == EVENT
    assert document["source_count"] == 6
    assert document["gate_passed"] is True
    assert document["claims"]["hil_acceptance"] is False
    assert [item["role"] for item in document["files"]] == list(files)
    assert verify_px4_runtime_capture_receipt(output, base_directory=base) == document

    schema = json.loads(
        (ROOT / "configs/schemas/px4-runtime-capture-receipt.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(document)
    first_bytes = output.read_bytes()
    build_px4_runtime_capture_receipt(summary_path, base, output, overwrite=True)
    assert output.read_bytes() == first_bytes


def test_receipt_detects_source_tampering_and_summary_tampering(tmp_path: Path) -> None:
    base, summary_path, files = _fixture(tmp_path)
    output = base / "capture-receipt.json"
    build_px4_runtime_capture_receipt(summary_path, base, output)

    files["join"].write_bytes(b"tampered-join\n")
    with pytest.raises(Px4RuntimeCaptureReceiptError, match="does not match"):
        verify_px4_runtime_capture_receipt(output, base_directory=base)

    files["join"].write_bytes(b"join\n")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["runtime_mode"] = "real"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    with pytest.raises(Px4RuntimeCaptureReceiptError, match="does not match"):
        verify_px4_runtime_capture_receipt(output, base_directory=base)


def test_receipt_rejects_failed_capture_gate_and_outside_source(tmp_path: Path) -> None:
    base, summary_path, files = _fixture(tmp_path)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["join"]["gate_passed"] = False
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    with pytest.raises(Px4RuntimeCaptureReceiptError, match="join gate"):
        build_px4_runtime_capture_receipt(summary_path, base, base / "receipt.json")

    outside = tmp_path / "outside.json"
    _write(outside, b"outside\n")
    summary["join"]["gate_passed"] = True
    summary["mission"]["path"] = str(outside)
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    with pytest.raises(Px4RuntimeCaptureReceiptError, match="inside base directory"):
        build_px4_runtime_capture_receipt(summary_path, base, base / "receipt.json")


def test_receipt_cli_create_and_verify(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base, summary_path, _ = _fixture(tmp_path)
    output = base / "cli-receipt.json"
    assert (
        main(
            [
                "--capture-summary",
                str(summary_path),
                "--base-directory",
                str(base),
                "--out",
                str(output),
                "--runtime-mode",
                "hil",
            ]
        )
        == 0
    )
    created = json.loads(capsys.readouterr().out)
    assert created["event"] == EVENT
    assert output.is_file()

    assert verify_main([str(output), "--base-directory", str(base), "--runtime-mode", "hil"]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["event"] == "px4_runtime_capture_receipt_verified"


def test_receipt_survives_relocation_without_original_directory(tmp_path: Path) -> None:
    base, summary_path, _ = _fixture(tmp_path)
    document = build_px4_runtime_capture_receipt(summary_path, base, "capture-receipt.json")
    summary_bytes = summary_path.read_bytes()
    destination = tmp_path / "copied capture"
    shutil.copytree(base, destination)
    base.rename(tmp_path / "original-offline")

    assert (destination / "capture-summary.json").read_bytes() == summary_bytes
    assert (
        verify_px4_runtime_capture_receipt(
            "capture-receipt.json", base_directory=destination, expected_runtime_mode="hil"
        )
        == document
    )
    assert verify_px4_runtime_capture_receipt(destination / "capture-receipt.json") == document
    with pytest.raises(Px4RuntimeCaptureReceiptError, match="runtime_mode"):
        verify_px4_runtime_capture_receipt(
            destination / "capture-receipt.json", expected_runtime_mode="real"
        )


@pytest.mark.parametrize(
    "role",
    [
        "mission",
        "runtime_observation",
        "runtime_profile",
        "mission_feedback",
        "endpoint_vehicle_identity",
        "join",
    ],
)
@pytest.mark.parametrize("mutation", ["missing", "same-size-tamper"])
def test_relocated_receipt_checks_destination_not_original(
    tmp_path: Path, role: str, mutation: str
) -> None:
    base, summary_path, files = _fixture(tmp_path)
    build_px4_runtime_capture_receipt(summary_path, base, "capture-receipt.json")
    destination = tmp_path / "copy"
    shutil.copytree(base, destination)
    target = destination / files[role].relative_to(base)
    if mutation == "missing":
        target.unlink()
    else:
        payload = target.read_bytes()
        target.write_bytes(bytes([payload[0] ^ 1]) + payload[1:])
    # The original remains valid; falling back to it must never mask damage.
    verify_px4_runtime_capture_receipt(base / "capture-receipt.json")
    with pytest.raises(Px4RuntimeCaptureReceiptError):
        verify_px4_runtime_capture_receipt(destination / "capture-receipt.json")


@pytest.mark.parametrize("origin", ["C:/offline/capture", "/offline/capture"])
def test_receipt_historical_paths_are_platform_independent(tmp_path: Path, origin: str) -> None:
    base, summary_path, _ = _fixture(tmp_path)
    document = build_px4_runtime_capture_receipt(summary_path, base, "capture-receipt.json")
    summary = json.loads(summary_path.read_bytes())
    for entry in document["files"]:
        summary[entry["role"]]["path"] = origin + "/" + entry["path"]
    payload = json.dumps(summary).encode()
    summary_path.write_bytes(payload)
    document["capture_summary"].update(bytes=len(payload), sha256=_sha256(payload))
    (base / "capture-receipt.json").write_text(json.dumps(document), encoding="utf-8")
    assert verify_px4_runtime_capture_receipt(base / "capture-receipt.json") == document


@pytest.mark.parametrize("mutation", ["wrong-role", "wrong-path", "mixed-origin"])
def test_receipt_rejects_inconsistent_origin_mapping(tmp_path: Path, mutation: str) -> None:
    base, summary_path, _ = _fixture(tmp_path)
    document = build_px4_runtime_capture_receipt(summary_path, base, "capture-receipt.json")
    if mutation == "wrong-role":
        document["files"][0]["role"] = "join"
    elif mutation == "wrong-path":
        shutil.copyfile(base / "mission.json", base / "other.json")
        document["files"][0]["path"] = "other.json"
    else:
        summary = json.loads(summary_path.read_bytes())
        summary["mission"]["path"] = str(tmp_path / "different-root" / "mission.json")
        payload = json.dumps(summary).encode()
        summary_path.write_bytes(payload)
        document["capture_summary"].update(bytes=len(payload), sha256=_sha256(payload))
    (base / "capture-receipt.json").write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(Px4RuntimeCaptureReceiptError):
        verify_px4_runtime_capture_receipt(base / "capture-receipt.json")
