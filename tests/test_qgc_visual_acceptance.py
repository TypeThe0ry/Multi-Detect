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
from multidetect.release_gate import _qgc_operator_visual_acceptance_result
from multidetect.release_gate_manifest import (
    ReleaseGateManifestError,
    _qgc_operator_visual_acceptance_record,
)

ROOT = Path(__file__).resolve().parents[1]


def _write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")


def _fixture(tmp_path: Path, runtime_mode: str = "hil") -> dict[str, Path]:
    screenshot = tmp_path / "artifacts" / runtime_mode / "qgc.png"
    screenshot.parent.mkdir(parents=True)
    screenshot.write_bytes(b"\x89PNG\r\n\x1a\nvisual-pixels-" + runtime_mode.encode("ascii"))
    executable = tmp_path / "build" / runtime_mode / "MultiDetectGCS.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"MZ\x00visual-qgc")
    parity = tmp_path / "configs" / "release-parity.json"
    ui_hash = "1" * 64
    _write_json(parity, {"ui_contract_hash": ui_hash})

    machine = tmp_path / "artifacts" / runtime_mode / "machine.json"
    _write_json(
        machine,
        {
            "schema": "multidetect.qgc-native-style-capture",
            "schema_version": 2,
            "event": "qgc_native_style_capture",
            "runtime_mode": runtime_mode,
            "run_id": f"visual-test-{runtime_mode}",
            "executable": str(executable),
            "executable_identity": {
                "path": str(executable),
                "bytes": executable.stat().st_size,
                "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
            },
            "release_parity": {
                "path": str(parity),
                "bytes": parity.stat().st_size,
                "sha256": hashlib.sha256(parity.read_bytes()).hexdigest(),
                "parity": {"ui_contract_hash": ui_hash},
            },
            "attached_existing_process": True,
            "requested_existing_process_id": 1234,
            "qgc_pid": 1234,
            "exact_client_rect_screen": {"width": 1600, "height": 900},
            "external_occluders_after": [],
            "single_native_surface_machine_check": True,
            "operator_visual_acceptance": "not_claimed",
            "error": None,
            "capture_gate_passed": True,
            "capture": {
                "path": str(screenshot),
                "bytes": screenshot.stat().st_size,
                "sha256": hashlib.sha256(screenshot.read_bytes()).hexdigest(),
            },
        },
    )
    negative = tmp_path / "artifacts" / runtime_mode / "negative.json"
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
        "runtime_mode": runtime_mode,
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
        runtime_mode=str(files["runtime_mode"]),
        notes="all controls readable",
    )


def _rewrite_as_legacy_px4_report(files: dict[str, Path], *, runtime_mode: str = "hil") -> None:
    current = json.loads(files["machine"].read_text(encoding="utf-8"))
    ui_hash = current["release_parity"]["parity"]["ui_contract_hash"]
    _write_json(
        files["machine"],
        {
            "event": "px4_sitl_qgc_operator_acceptance",
            "runtime_mode": runtime_mode,
            "run_id": f"legacy-{runtime_mode}",
            "qgc": {
                "executable": str(files["executable"]),
                "executable_sha256": hashlib.sha256(files["executable"].read_bytes()).hexdigest(),
                "isolated_hil_cli": True,
                "native_visual_machine_capture": {
                    "qgc_pid": 1234,
                    "exact_client_rect_screen": {"width": 1600, "height": 900},
                    "single_native_surface_machine_check": True,
                    "operator_visual_acceptance": "not_claimed",
                    "error": None,
                    "capture": current["capture"],
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


def _rewrite_as_combined_sim_report(files: dict[str, Path]) -> None:
    current = json.loads(files["machine"].read_text(encoding="utf-8"))
    parity = json.loads(files["parity"].read_text(encoding="utf-8"))
    _write_json(
        files["machine"],
        {
            "schema": "multidetect.px4-gazebo-qgc-combined-sim-acceptance",
            "schema_version": 2,
            "runtime_mode": "sim",
            "run_id": "actual-combined-sim",
            "gate_passed": True,
            "hashes": {
                "qgc_exe": current["executable_identity"],
                "release_parity": {
                    "path": str(files["parity"]),
                    "bytes": files["parity"].stat().st_size,
                    "sha256": hashlib.sha256(files["parity"].read_bytes()).hexdigest(),
                    "parity": parity,
                },
            },
            "integrated_sim_qgc": {
                "runtime_mode": "sim",
                "qgc_transport_acceptance_gate": (
                    "passed_rtsp_frame_ack_and_runtime_status_post_adoption_ack"
                ),
                "synchronization": {
                    "complete": True,
                    "metadata_acceptance_ack_count": 12,
                },
            },
            "qgc": {
                "expected_runtime_mode": "SIM",
                "runtime_status_run_hash_bound_to_integrated_identity": True,
                "native_toolbar_indicator": {
                    "gate_passed": True,
                    "operator_visual_acceptance": "not_claimed",
                    "geometry_error": None,
                    "automation_error": None,
                    "qgc_process": {
                        "pid": 1234,
                        "client_screen_rect": {"width": 1600, "height": 900},
                    },
                    "open_capture": {
                        **current["capture"],
                        "width": 1600,
                        "height": 900,
                    },
                    "single_surface": {"unchanged_and_only_main_window": True},
                    "checks": {
                        "indicator_geometry_bound_to_exact_qgc_pid_hwnd": True,
                        "drawer_open_closed_visual_change": True,
                    },
                },
            },
        },
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

    machine_schema = json.loads(
        (ROOT / "configs/schemas/qgc-native-style-capture.schema.json").read_text(encoding="utf-8")
    )
    machine = json.loads(files["machine"].read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(machine_schema)
    Draft202012Validator(machine_schema, format_checker=FormatChecker()).validate(machine)


def test_legacy_px4_hil_machine_report_remains_accepted(tmp_path: Path) -> None:
    files = _fixture(tmp_path)
    _rewrite_as_legacy_px4_report(files)
    document = _accepted(files)
    assert document["gate_passed"] is True
    assert "schema" not in document["positive_visual_evidence"]["machine_report"]


def test_legacy_px4_hil_machine_report_cannot_claim_sim(tmp_path: Path) -> None:
    files = _fixture(tmp_path, "sim")
    _rewrite_as_legacy_px4_report(files, runtime_mode="sim")
    document = _accepted(files)
    assert document["gate_passed"] is False
    assert document["positive_visual_evidence"]["machine_checks"]["machine_event"] is False


def test_combined_sim_runtime_report_is_accepted_and_reproducible(tmp_path: Path) -> None:
    files = _fixture(tmp_path, "sim")
    _rewrite_as_combined_sim_report(files)
    document = _accepted(files)
    assert document["gate_passed"] is True
    machine_binding = document["positive_visual_evidence"]["machine_report"]
    assert machine_binding["runtime_mode"] == "sim"
    assert (
        machine_binding["event"]
        == "multidetect.px4-gazebo-qgc-combined-sim-acceptance"
    )
    evidence = tmp_path / "artifacts" / "sim" / "combined-visual-review.json"
    _write_json(evidence, document)
    verification = verify_record(evidence, repository_root=tmp_path)
    assert verification["gate_passed"] is True
    assert all(verification["checks"].values())


def test_combined_sim_report_cannot_relabel_replay_as_sim(tmp_path: Path) -> None:
    files = _fixture(tmp_path, "sim")
    _rewrite_as_combined_sim_report(files)
    machine = json.loads(files["machine"].read_text(encoding="utf-8"))
    machine["integrated_sim_qgc"]["runtime_mode"] = "replay"
    _write_json(files["machine"], machine)
    document = _accepted(files)
    assert document["gate_passed"] is False
    assert document["positive_visual_evidence"]["machine_checks"]["machine_event"] is False


def test_combined_sim_report_requires_qgc_post_adoption_ack(tmp_path: Path) -> None:
    files = _fixture(tmp_path, "sim")
    _rewrite_as_combined_sim_report(files)
    machine = json.loads(files["machine"].read_text(encoding="utf-8"))
    machine["integrated_sim_qgc"]["synchronization"][
        "metadata_acceptance_ack_count"
    ] = 0
    _write_json(files["machine"], machine)
    document = _accepted(files)
    assert document["gate_passed"] is False
    assert (
        document["positive_visual_evidence"]["machine_checks"]["capture_bound_to_pid"]
        is False
    )


def test_combined_sim_report_with_stale_release_parity_remains_closed(tmp_path: Path) -> None:
    files = _fixture(tmp_path, "sim")
    _rewrite_as_combined_sim_report(files)
    machine = json.loads(files["machine"].read_text(encoding="utf-8"))
    machine["hashes"]["release_parity"]["parity"]["ui_contract_hash"] = "2" * 64
    _write_json(files["machine"], machine)
    document = _accepted(files)
    assert document["gate_passed"] is False
    checks = document["positive_visual_evidence"]["machine_checks"]
    assert checks["release_parity_values_match_current"] is False
    assert checks["ui_contract_matches_current"] is False


def test_native_capture_with_occluder_remains_closed(tmp_path: Path) -> None:
    files = _fixture(tmp_path, "real")
    machine = json.loads(files["machine"].read_text(encoding="utf-8"))
    machine["external_occluders_after"] = [{"pid": 99, "title": "cover"}]
    _write_json(files["machine"], machine)
    document = _accepted(files)
    assert document["gate_passed"] is False
    assert document["positive_visual_evidence"]["machine_checks"]["no_external_occluders"] is False


def test_native_capture_stale_embedded_ui_contract_remains_closed(tmp_path: Path) -> None:
    files = _fixture(tmp_path, "replay")
    machine = json.loads(files["machine"].read_text(encoding="utf-8"))
    machine["release_parity"]["parity"]["ui_contract_hash"] = "2" * 64
    _write_json(files["machine"], machine)
    document = _accepted(files)
    assert document["gate_passed"] is False
    assert (
        document["positive_visual_evidence"]["machine_checks"]["ui_contract_matches_current"]
        is False
    )


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
            runtime_mode="hil",
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
        runtime_mode="hil",
    )
    assert document["gate_passed"] is False
    assert set(document["reasons"]) == {"decision_accepted", "checklist_complete"}


def test_template_cli_is_fail_closed(tmp_path: Path) -> None:
    output = tmp_path / "checklist.json"
    assert main(["template", "--out", str(output)]) == 0
    assert json.loads(output.read_text(encoding="utf-8")) == checklist_template()


def _four_mode_records(tmp_path: Path) -> list[Path]:
    records: list[Path] = []
    for mode in ("replay", "sim", "hil", "real"):
        files = _fixture(tmp_path, mode)
        evidence = tmp_path / "artifacts" / mode / "visual-review.json"
        _write_json(evidence, _accepted(files))
        records.append(evidence)
    return records


def test_four_mode_release_gate_binding_is_reproducible(tmp_path: Path) -> None:
    records = _four_mode_records(tmp_path)
    aggregate = _qgc_operator_visual_acceptance_record(
        records,
        output_identity=(tmp_path / "release-gate.json").resolve(),
        manifest_directory=tmp_path.resolve(),
    )
    assert aggregate["complete"] is True
    assert aggregate["gate_passed"] is True
    assert aggregate["covered_modes"] == ["replay", "sim", "hil", "real"]

    result = _qgc_operator_visual_acceptance_result(
        aggregate,
        manifest_directory=tmp_path.resolve(),
    )
    assert result["passed"] is True
    assert result["complete"] is True
    assert all(record["passed"] for record in result["records"])

    release_schema = json.loads(
        (ROOT / "configs/schemas/release-gate-manifest.schema.json").read_text(encoding="utf-8")
    )
    aggregate_schema = {
        "$schema": release_schema["$schema"],
        "$ref": "#/$defs/qgcOperatorVisualAcceptanceSetRecord",
        "$defs": release_schema["$defs"],
    }
    Draft202012Validator.check_schema(aggregate_schema)
    Draft202012Validator(
        aggregate_schema,
        format_checker=FormatChecker(),
    ).validate(aggregate)


def test_partial_four_mode_set_remains_fail_closed(tmp_path: Path) -> None:
    records = _four_mode_records(tmp_path)[:-1]
    aggregate = _qgc_operator_visual_acceptance_record(
        records,
        output_identity=(tmp_path / "release-gate.json").resolve(),
        manifest_directory=tmp_path.resolve(),
    )
    assert aggregate["complete"] is False
    assert aggregate["gate_passed"] is False

    result = _qgc_operator_visual_acceptance_result(
        aggregate,
        manifest_directory=tmp_path.resolve(),
    )
    assert result["passed"] is False
    assert "all four runtime-mode visual reviews are required" in result["reasons"]


def test_release_gate_reopens_and_rejects_tampered_visual_record(tmp_path: Path) -> None:
    records = _four_mode_records(tmp_path)
    aggregate = _qgc_operator_visual_acceptance_record(
        records,
        output_identity=(tmp_path / "release-gate.json").resolve(),
        manifest_directory=tmp_path.resolve(),
    )
    records[0].write_bytes(records[0].read_bytes() + b"\n")
    result = _qgc_operator_visual_acceptance_result(
        aggregate,
        manifest_directory=tmp_path.resolve(),
    )
    assert result["passed"] is False
    assert any("evidence SHA-256 does not match" in reason for reason in result["reasons"])


def test_machine_runtime_mode_mismatch_keeps_visual_record_closed(tmp_path: Path) -> None:
    files = _fixture(tmp_path, "sim")
    document = create_record(
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
        runtime_mode="real",
    )
    assert document["gate_passed"] is False
    assert document["positive_visual_evidence"]["machine_checks"]["runtime_mode_matches"] is False


def test_release_gate_rejects_cross_mode_source_reuse(tmp_path: Path) -> None:
    records = _four_mode_records(tmp_path)
    aggregate = _qgc_operator_visual_acceptance_record(
        records,
        output_identity=(tmp_path / "release-gate.json").resolve(),
        manifest_directory=tmp_path.resolve(),
    )
    aggregate["records"][1]["screenshot_sha256"] = aggregate["records"][0]["screenshot_sha256"]
    result = _qgc_operator_visual_acceptance_result(
        aggregate,
        manifest_directory=tmp_path.resolve(),
    )
    assert result["passed"] is False
    assert any(
        "screenshot is reused across runtime modes" in reason for reason in result["reasons"]
    )


def test_manifest_builder_rejects_same_screenshot_across_modes(tmp_path: Path) -> None:
    sim = _fixture(tmp_path, "sim")
    hil = _fixture(tmp_path, "hil")
    hil_machine = json.loads(hil["machine"].read_text(encoding="utf-8"))
    hil_capture = hil_machine["capture"]
    hil_capture.update(
        {
            "path": str(sim["screenshot"]),
            "bytes": sim["screenshot"].stat().st_size,
            "sha256": hashlib.sha256(sim["screenshot"].read_bytes()).hexdigest(),
        }
    )
    _write_json(hil["machine"], hil_machine)

    sim_record = tmp_path / "artifacts" / "sim" / "visual-review.json"
    hil_record = tmp_path / "artifacts" / "hil" / "visual-review.json"
    _write_json(sim_record, _accepted(sim))
    hil_document = create_record(
        repository_root=hil["root"],
        machine_report=hil["machine"],
        screenshot=sim["screenshot"],
        qgc_executable=hil["executable"],
        release_parity=hil["parity"],
        negative_control=hil["negative"],
        reviewer="Test Operator",
        reviewed_at_utc="2026-09-05T14:00:00Z",
        checklist={name: True for name in REQUIRED_CHECKS},
        decision="accepted",
        runtime_mode="hil",
    )
    assert hil_document["gate_passed"] is True
    _write_json(hil_record, hil_document)

    try:
        _qgc_operator_visual_acceptance_record(
            [sim_record, hil_record],
            output_identity=(tmp_path / "release-gate.json").resolve(),
            manifest_directory=tmp_path.resolve(),
        )
    except ReleaseGateManifestError as exc:
        assert "reuses one screenshot across runtime modes" in str(exc)
    else:
        raise AssertionError("cross-mode screenshot reuse unexpectedly accepted")
