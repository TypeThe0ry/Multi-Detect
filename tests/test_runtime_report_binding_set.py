from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from multidetect.runtime import RuntimeRunReport, write_runtime_run_report
from multidetect.runtime_report_binding_set import (
    DEFAULT_REQUIRED_MODES,
    SCHEMA,
    RuntimeReportBindingSetError,
    main,
    verify_runtime_report_binding_set,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "configs/schemas/runtime-report-binding-set.schema.json"


def _write_json(path: Path, document: object) -> Path:
    path.write_text(json.dumps(document, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path


def _replay_fixture(tmp_path: Path) -> tuple[Path, Path]:
    report = RuntimeRunReport(
        descriptor_hash="a" * 64,
        input_hash="b" * 64,
        processed_frame_ids=("frame-0001", "frame-0002"),
        started_at_s=0.0,
        finished_at_s=2.0,
        stream_exhausted=True,
        max_frames=None,
    )
    report_path = write_runtime_run_report(tmp_path / "replay-report.json", report)
    result_path = _write_json(
        tmp_path / "replay-result.json",
        {
            "schema_version": 2,
            "event": "integrated_replay_qgc_finished",
            "runtime_mode": "replay",
            "gate_passed": True,
            "recording_hash": report.input_hash,
            "runtime_descriptor_hash": report.descriptor_hash,
            "ground_truth_isolated": True,
            "result": {"processed_frames": 2},
            "runtime_run_report": report.to_mapping_with_hash(),
        },
    )
    return result_path, report_path


def _sim_fixture(tmp_path: Path) -> tuple[Path, Path]:
    report = RuntimeRunReport(
        descriptor_hash="c" * 64,
        input_hash="d" * 64,
        processed_frame_ids=("sim:demo:0", "sim:demo:1"),
        started_at_s=0.0,
        finished_at_s=0.2,
        stream_exhausted=False,
        max_frames=2,
    )
    report_path = write_runtime_run_report(tmp_path / "sim-report.json", report)
    result_path = _write_json(
        tmp_path / "sim-result.json",
        {
            "schema_version": 1,
            "event": "simulator_scenario_run_finished",
            "scenario_id": "demo",
            "scenario_manifest_hash": "e" * 64,
            "runtime_input_hash": report.input_hash,
            "runtime_descriptor_hash": report.descriptor_hash,
            "requested_frames": 2,
            "completed_frames": 2,
            "terminal_reason": "frame_budget_complete",
            "truth_free": True,
            "closed": True,
        },
    )
    return result_path, report_path


def _observation_fixture(tmp_path: Path, mode: str) -> tuple[Path, Path]:
    descriptor_hash = ("1" if mode == "hil" else "2") * 64
    input_hash = ("3" if mode == "hil" else "4") * 64
    frame_ids = (f"{mode}:frame-0001", f"{mode}:frame-0002")
    report = RuntimeRunReport(
        descriptor_hash=descriptor_hash,
        input_hash=input_hash,
        processed_frame_ids=frame_ids,
        started_at_s=20.0,
        finished_at_s=20.2,
        stream_exhausted=False,
        max_frames=2,
    )
    report_path = write_runtime_run_report(tmp_path / f"{mode}-report.json", report)
    result_path = _write_json(
        tmp_path / f"{mode}-result.json",
        {
            "schema_version": 1,
            "event": f"{mode}_runtime_observation_finished",
            "runtime_mode": mode,
            "gate_passed": True,
            "runtime_descriptor_hash": descriptor_hash,
            "runtime_input_hash": input_hash,
            "processed_frame_count": 2,
            "processed_frame_ids": list(frame_ids),
            "stream_exhausted": False,
            "max_frames": 2,
            "observation_only": True,
            "read_only": True,
            "receive_only": True,
            "flight_control_enabled": False,
            "hardware_outputs_present": False,
            "messages_transmitted": 0,
            "output_locked": True,
            "runtime_run_report": report.to_mapping_with_hash(),
        },
    )
    return result_path, report_path


def _spec(mode: str, result: Path, report: Path) -> dict[str, str]:
    return {
        "runtime_mode": mode,
        "result_path": str(result),
        "runtime_report_path": str(report),
    }


def test_replay_and_sim_set_is_complete_and_schema_valid(tmp_path: Path) -> None:
    replay_result, replay_report = _replay_fixture(tmp_path)
    sim_result, sim_report = _sim_fixture(tmp_path)

    document = verify_runtime_report_binding_set(
        [_spec("sim", sim_result, sim_report), _spec("replay", replay_result, replay_report)],
        required_modes=("replay", "sim"),
        base_directory=tmp_path,
    )

    assert document["schema"] == SCHEMA
    assert document["covered_modes"] == ["replay", "sim"]
    assert document["binding_count"] == 2
    assert document["complete"] is True
    assert document["gate_passed"] is True
    assert all(document["checks"].values())
    assert [entry["runtime_mode"] for entry in document["bindings"]] == ["replay", "sim"]
    assert all("D:" not in entry["result"]["path"] for entry in document["bindings"])

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(document)


def test_default_four_mode_set_remains_closed_until_hil_and_real_exist(tmp_path: Path) -> None:
    replay_result, replay_report = _replay_fixture(tmp_path)
    sim_result, sim_report = _sim_fixture(tmp_path)

    document = verify_runtime_report_binding_set(
        [_spec("replay", replay_result, replay_report), _spec("sim", sim_result, sim_report)],
        base_directory=tmp_path,
    )

    assert tuple(document["required_modes"]) == DEFAULT_REQUIRED_MODES
    assert document["complete"] is False
    assert document["gate_passed"] is False
    assert document["checks"]["required_modes_complete"] is False
    assert document["checks"]["all_bindings_passed"] is True


def test_four_mode_set_accepts_explicit_hil_real_observation_pairs(tmp_path: Path) -> None:
    replay_result, replay_report = _replay_fixture(tmp_path)
    sim_result, sim_report = _sim_fixture(tmp_path)
    hil_result, hil_report = _observation_fixture(tmp_path, "hil")
    real_result, real_report = _observation_fixture(tmp_path, "real")

    document = verify_runtime_report_binding_set(
        [
            _spec("real", real_result, real_report),
            _spec("sim", sim_result, sim_report),
            _spec("hil", hil_result, hil_report),
            _spec("replay", replay_result, replay_report),
        ],
        base_directory=tmp_path,
        require_embedded_modes=("replay", "hil", "real"),
    )

    assert document["required_modes"] == ["replay", "sim", "hil", "real"]
    assert document["covered_modes"] == ["replay", "sim", "hil", "real"]
    assert document["binding_count"] == 4
    assert document["complete"] is True
    assert document["gate_passed"] is True
    assert all(document["checks"].values())
    assert all(entry["checks"]["output_safety_contract"] is True for entry in document["bindings"])

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(document)


def test_hil_real_observation_pair_with_transmit_is_rejected_by_set(tmp_path: Path) -> None:
    hil_result, hil_report = _observation_fixture(tmp_path, "hil")
    result = json.loads(hil_result.read_text(encoding="utf-8"))
    result["messages_transmitted"] = 1
    _write_json(hil_result, result)

    document = verify_runtime_report_binding_set(
        [_spec("hil", hil_result, hil_report)],
        required_modes=("hil",),
        base_directory=tmp_path,
    )

    entry = document["bindings"][0]
    assert document["gate_passed"] is False
    assert entry["gate_passed"] is False
    assert entry["checks"]["output_safety_contract"] is False
    assert entry["checks"]["result_gate_passed"] is False


def test_optional_bound_mode_does_not_break_required_mode_coverage(tmp_path: Path) -> None:
    replay_result, replay_report = _replay_fixture(tmp_path)
    sim_result, sim_report = _sim_fixture(tmp_path)

    document = verify_runtime_report_binding_set(
        [_spec("replay", replay_result, replay_report), _spec("sim", sim_result, sim_report)],
        required_modes=("replay",),
        base_directory=tmp_path,
    )

    assert document["covered_modes"] == ["replay", "sim"]
    assert document["complete"] is True
    assert document["gate_passed"] is True
    assert document["checks"]["required_modes_complete"] is True


def test_duplicate_mode_and_source_reuse_fail_closed(tmp_path: Path) -> None:
    replay_result, replay_report = _replay_fixture(tmp_path)
    document = verify_runtime_report_binding_set(
        [
            _spec("replay", replay_result, replay_report),
            _spec("replay", replay_result, replay_report),
        ],
        required_modes=("replay",),
        base_directory=tmp_path,
    )

    assert document["gate_passed"] is False
    assert document["checks"]["entries_unique"] is False
    assert document["checks"]["result_paths_distinct"] is False
    assert document["checks"]["runtime_report_paths_distinct"] is False
    assert document["checks"]["source_paths_distinct"] is False


def test_symbolic_link_source_is_rejected_before_resolution(tmp_path: Path) -> None:
    replay_result, replay_report = _replay_fixture(tmp_path)
    result_link = tmp_path / "replay-result-link.json"
    try:
        result_link.symlink_to(replay_result)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symbolic links unavailable: {exc}")

    with pytest.raises(RuntimeReportBindingSetError, match="must not be a symbolic link"):
        verify_runtime_report_binding_set(
            [_spec("replay", result_link, replay_report)],
            required_modes=("replay",),
            base_directory=tmp_path,
        )


def test_empty_source_is_rejected_as_structural_error(tmp_path: Path) -> None:
    empty_result = tmp_path / "empty-result.json"
    empty_result.write_bytes(b"")
    report = tmp_path / "report.json"
    report.write_text("{}", encoding="utf-8")

    with pytest.raises(RuntimeReportBindingSetError, match="source file is empty"):
        verify_runtime_report_binding_set(
            [_spec("replay", empty_result, report)],
            required_modes=("replay",),
            base_directory=tmp_path,
        )


def test_tampered_pair_is_reported_as_entry_error(tmp_path: Path) -> None:
    replay_result, replay_report = _replay_fixture(tmp_path)
    report = json.loads(replay_report.read_text(encoding="utf-8"))
    report["processed_frame_count"] = 99
    _write_json(replay_report, report)

    document = verify_runtime_report_binding_set(
        [_spec("replay", replay_result, replay_report)],
        required_modes=("replay",),
        base_directory=tmp_path,
    )

    entry = document["bindings"][0]
    assert document["gate_passed"] is False
    assert document["checks"]["all_bindings_passed"] is False
    assert document["checks"]["no_errors"] is False
    assert entry["gate_passed"] is False
    assert entry["error"]["type"] == "RuntimeReportBindingError"


def test_cli_writes_schema_valid_set_report(tmp_path: Path, capsys) -> None:
    replay_result, replay_report = _replay_fixture(tmp_path)
    output = tmp_path / "binding-set.json"

    exit_code = main(
        [
            "--required-modes",
            "replay",
            "--base-directory",
            str(tmp_path),
            "--binding",
            "replay",
            replay_result.name,
            replay_report.name,
            "--out",
            str(output),
        ]
    )

    assert exit_code == 0
    document = json.loads(output.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(document)
    assert json.loads(capsys.readouterr().out)["gate_passed"] is True


def test_cli_structural_error_is_schema_valid(tmp_path: Path, capsys) -> None:
    output = tmp_path / "binding-set-error.json"
    exit_code = main(
        [
            "--required-modes",
            "replay",
            "--base-directory",
            str(tmp_path),
            "--binding",
            "invalid-mode",
            "result.json",
            "report.json",
            "--out",
            str(output),
        ]
    )

    assert exit_code == 1
    document = json.loads(output.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(document)
    assert document["event"] == "runtime_report_binding_set_error"
    assert document["gate_passed"] is False
    assert document["error"]["type"] == "RuntimeReportBindingSetError"
    assert json.loads(capsys.readouterr().out)["event"] == "runtime_report_binding_set_error"
