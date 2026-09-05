from __future__ import annotations

import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from multidetect.qgc_visual_acceptance import (
    ATTESTATION,
    REQUIRED_CHECKS,
    QgcVisualAcceptanceError,
    checklist_template,
    create_record,
    main,
    verify_record,
)

ROOT = Path(__file__).resolve().parents[1]


def _write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")


def _fixture(tmp_path: Path) -> dict[str, Path]:
    screenshot = tmp_path / "artifacts" / "qgc.png"
    screenshot.parent.mkdir(parents=True)
    screenshot.write_bytes(b"\x89PNG\r\n\x1a\nvisual-pixels")
    executable = tmp_path / "build" / "MultiDetectGCS.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"MZ\x00visual-qgc")
    parity = tmp_path / "configs" / "release-parity.json"
    ui_hash = "1" * 64
    _write_json(parity, {"ui_contract_hash": ui_hash})

    machine = tmp_path / "artifacts" / "machine.json"
    _write_json(
        machine,
        {
            "event": "px4_sitl_qgc_operator_acceptance",
            "run_id": "visual-test-1",
            "qgc": {
                "executable": str(executable),
                "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
                "native_visual_machine_capture": {
                    "qgc_pid": 1234,
                    "exact_client_rect_screen": {"width": 1600, "height": 900},
                    "single_native_surface_machine_check": True,
                    "operator_visual_acceptance": "not_claimed",
                    "error": None,
                    "capture": {
                        "path": str(screenshot),
                        "bytes": screenshot.stat().st_size,
                        "sha256": hashlib.sha256(screenshot.read_bytes()).hexdigest(),
                    },
                },
            },
            "assertions": {
                "all_passed": True,
                "native_visual_capture_bound_to_acceptance_pid": True,
            },
            "jetson_metadata_hil": {
                "target_geolocation_evidence": {
                    "expected_release_parity": {"parity": {"ui_contract_hash": ui_hash}}
                }
            },
        },
    )
    negative = tmp_path / "artifacts" / "negative.json"
    frames = [
        {
            "detections": [{"label": "car"}],
            "predicted_target": None,
            "range_solution": {
                "validity": "invalid",
                "reasons": ["target_ray_does_not_intersect_ground_safely"],
            },
        }
        for _ in range(5)
    ]
    _write_json(
        negative,
        {
            "schema": "multidetect.gazebo-target-geolocation-acceptance",
            "schema_version": 2,
            "gate_passed": False,
            "frames": frames,
        },
    )
    return {
        "root": tmp_path,
        "screenshot": screenshot,
        "executable": executable,
        "parity": parity,
        "machine": machine,
        "negative": negative,
    }


def _accepted(files: dict[str, Path]) -> dict[str, object]:
    return create_record(
        repository_root=files["root"],
        machine_report=files["machine"],
        screenshot=files["screenshot"],
        qgc_executable=files["executable"],
        release_parity=files["parity"],
        negative_control=files["negative"],
        reviewer="Test Operator",
        reviewed_at_utc="2026-09-05T14:00:00Z",
        checklist={name: True for name in REQUIRED_CHECKS},
        decision="accepted",
        notes="all controls readable",
    )


def test_accepted_record_is_schema_valid_and_reproducible(tmp_path: Path) -> None:
    files = _fixture(tmp_path)
    document = _accepted(files)
    assert document["gate_passed"] is True
    assert document["review"]["attestation"] == ATTESTATION
    evidence = tmp_path / "artifacts" / "review.json"
    _write_json(evidence, document)
    verification = verify_record(evidence, repository_root=tmp_path)
    assert verification["gate_passed"] is True
    assert all(verification["checks"].values())

    schema = json.loads(
        (ROOT / "configs/schemas/qgc-operator-visual-acceptance.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)


def test_screenshot_tamper_fails_independent_verification(tmp_path: Path) -> None:
    files = _fixture(tmp_path)
    evidence = tmp_path / "artifacts" / "review.json"
    _write_json(evidence, _accepted(files))
    files["screenshot"].write_bytes(files["screenshot"].read_bytes() + b"tamper")
    verification = verify_record(evidence, repository_root=tmp_path)
    assert verification["gate_passed"] is False
    assert verification["checks"]["screenshot_file"] is False
    assert verification["checks"]["machine_binding_reproduced"] is False


def test_record_tamper_fails_integrity_digest(tmp_path: Path) -> None:
    files = _fixture(tmp_path)
    document = _accepted(files)
    document["review"]["reviewer"] = "Different Operator"
    evidence = tmp_path / "artifacts" / "review.json"
    _write_json(evidence, document)
    verification = verify_record(evidence, repository_root=tmp_path)
    assert verification["gate_passed"] is False
    assert verification["checks"]["review_sha256"] is False


def test_negative_control_false_point_keeps_gate_closed(tmp_path: Path) -> None:
    files = _fixture(tmp_path)
    negative = json.loads(files["negative"].read_text(encoding="utf-8"))
    negative["frames"][0]["predicted_target"] = {"latitude_deg": 1.0}
    _write_json(files["negative"], negative)
    document = _accepted(files)
    assert document["gate_passed"] is False
    assert "bound_sources_valid" in document["reasons"]
    assert (
        document["negative_geolocation_control"]["checks"]["zero_false_geolocation_points"] is False
    )


def test_incomplete_checklist_is_rejected(tmp_path: Path) -> None:
    files = _fixture(tmp_path)
    checklist = {name: True for name in REQUIRED_CHECKS}
    checklist.pop("record_control_visible")
    try:
        create_record(
            repository_root=files["root"],
            machine_report=files["machine"],
            screenshot=files["screenshot"],
            qgc_executable=files["executable"],
            release_parity=files["parity"],
            negative_control=files["negative"],
            reviewer="Test Operator",
            reviewed_at_utc="2026-09-05T14:00:00Z",
            checklist=checklist,
            decision="accepted",
        )
    except QgcVisualAcceptanceError as exc:
        assert "checklist keys must match exactly" in str(exc)
    else:
        raise AssertionError("incomplete checklist unexpectedly accepted")


def test_rejected_decision_writes_closed_record(tmp_path: Path) -> None:
    files = _fixture(tmp_path)
    document = create_record(
        repository_root=files["root"],
        machine_report=files["machine"],
        screenshot=files["screenshot"],
        qgc_executable=files["executable"],
        release_parity=files["parity"],
        negative_control=files["negative"],
        reviewer="Test Operator",
        reviewed_at_utc="2026-09-05T14:00:00Z",
        checklist=checklist_template(),
        decision="rejected",
    )
    assert document["gate_passed"] is False
    assert set(document["reasons"]) == {"decision_accepted", "checklist_complete"}


def test_template_cli_is_fail_closed(tmp_path: Path) -> None:
    output = tmp_path / "checklist.json"
    assert main(["template", "--out", str(output)]) == 0
    assert json.loads(output.read_text(encoding="utf-8")) == checklist_template()
