from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from multidetect.runtime import RuntimeRunReport, write_runtime_run_report
from multidetect.runtime_report_binding import (
    SCHEMA,
    main,
    verify_runtime_report_binding,
)
from multidetect.simulator.scenario_runner import (
    SimulatorScenarioRunner,
    SimulatorScenarioRunnerConfig,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "configs" / "schemas" / "runtime-report-binding.schema.json"
SIM_MANIFEST = ROOT / "configs" / "scenarios" / "river_rescue_vtol_001.scenario.json"


def _write_result(path: Path, document: dict[str, object]) -> Path:
    path.write_text(json.dumps(document, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path


def _integrated_fixture(tmp_path: Path) -> tuple[Path, Path, RuntimeRunReport]:
    report = RuntimeRunReport(
        descriptor_hash="a" * 64,
        input_hash="b" * 64,
        processed_frame_ids=("frame-0001", "frame-0002"),
        started_at_s=0.0,
        finished_at_s=2.0,
        stream_exhausted=True,
        max_frames=None,
    )
    report_path = write_runtime_run_report(tmp_path / "runtime-report.json", report)
    result_path = _write_result(
        tmp_path / "integrated-result.json",
        {
            "schema_version": 2,
            "event": "integrated_replay_qgc_finished",
            "runtime_mode": "replay",
            "gate_passed": True,
            "recording_hash": "b" * 64,
            "runtime_descriptor_hash": "a" * 64,
            "ground_truth_isolated": True,
            "result": {"processed_frames": 2},
            "runtime_run_report": report.to_mapping_with_hash(),
        },
    )
    return result_path, report_path, report


def _observation_fixture(tmp_path: Path, mode: str) -> tuple[Path, Path, RuntimeRunReport]:
    descriptor_hash = ("c" if mode == "hil" else "d") * 64
    input_hash = ("e" if mode == "hil" else "f") * 64
    frame_ids = (f"{mode}:frame-0001", f"{mode}:frame-0002")
    report = RuntimeRunReport(
        descriptor_hash=descriptor_hash,
        input_hash=input_hash,
        processed_frame_ids=frame_ids,
        started_at_s=10.0,
        finished_at_s=10.2,
        stream_exhausted=False,
        max_frames=2,
    )
    report_path = write_runtime_run_report(tmp_path / f"{mode}-runtime-report.json", report)
    result_path = _write_result(
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
    return result_path, report_path, report


def test_integrated_result_and_sidecar_bind_end_to_end(tmp_path: Path) -> None:
    result_path, report_path, report = _integrated_fixture(tmp_path)

    binding = verify_runtime_report_binding(result_path, report_path)

    assert binding["schema"] == SCHEMA
    assert binding["gate_passed"] is True
    assert binding["runtime_report"]["canonical_sha256"] == report.canonical_hash
    assert all(binding["checks"].values())
    assert binding["expectations"]["embedded_report_present"] is True


def test_integrated_legacy_top_level_result_shape_binds(tmp_path: Path) -> None:
    """Bind artifacts emitted before the explicit nested result/gate fields."""

    _, report_path, report = _integrated_fixture(tmp_path)
    result_path = _write_result(
        tmp_path / "legacy-integrated-result.json",
        {
            "schema_version": 1,
            "event": "integrated_replay_finished",
            "runtime_mode": "replay",
            "recording_hash": report.input_hash,
            "runtime_descriptor_hash": report.descriptor_hash,
            "processed_frames": report.processed_frame_count,
            "frame_ids": list(report.processed_frame_ids),
            "natural_stream_exhaustion": True,
            "hardware_outputs_present": False,
            "flight_control_enabled": False,
            "runtime_run_report": report.to_mapping_with_hash(),
        },
    )

    binding = verify_runtime_report_binding(
        result_path,
        report_path,
        expected_runtime_mode="replay",
        require_embedded_report=True,
    )

    assert binding["gate_passed"] is True
    assert binding["checks"]["result_gate_passed"] is True
    assert binding["checks"]["processed_frame_ids_match"] is True


def test_integrated_binding_rejects_malformed_top_level_frame_ids(tmp_path: Path) -> None:
    _, report_path, report = _integrated_fixture(tmp_path)
    result_path = _write_result(
        tmp_path / "malformed-integrated-result.json",
        {
            "event": "integrated_replay_finished",
            "runtime_mode": "replay",
            "recording_hash": report.input_hash,
            "runtime_descriptor_hash": report.descriptor_hash,
            "processed_frames": report.processed_frame_count,
            "frame_ids": ["frame-0001", 2],
            "natural_stream_exhaustion": True,
            "hardware_outputs_present": False,
            "flight_control_enabled": False,
            "runtime_run_report": report.to_mapping_with_hash(),
        },
    )

    binding = verify_runtime_report_binding(result_path, report_path)

    assert binding["gate_passed"] is False
    assert binding["checks"]["processed_frame_ids_match"] is False
    assert "processed_frame_ids_match" in binding["reasons"]


def test_integrated_binding_fails_when_embedded_report_is_tampered(tmp_path: Path) -> None:
    result_path, report_path, _ = _integrated_fixture(tmp_path)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["runtime_run_report"]["processed_frame_count"] = 99
    result_path.write_text(json.dumps(result), encoding="utf-8")

    binding = verify_runtime_report_binding(result_path, report_path)

    assert binding["gate_passed"] is False
    assert binding["checks"]["embedded_report_matches"] is False
    assert "embedded_report_matches" in binding["reasons"]


def test_integrated_binding_rejects_declared_runtime_mode_drift(tmp_path: Path) -> None:
    result_path, report_path, _ = _integrated_fixture(tmp_path)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["runtime_mode"] = "sim"
    result_path.write_text(json.dumps(result), encoding="utf-8")

    binding = verify_runtime_report_binding(result_path, report_path)

    assert binding["gate_passed"] is False
    assert binding["checks"]["runtime_mode_matches"] is False


def test_integrated_binding_requires_processed_count(tmp_path: Path) -> None:
    result_path, report_path, _ = _integrated_fixture(tmp_path)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["result"].pop("processed_frames")
    result_path.write_text(json.dumps(result), encoding="utf-8")

    binding = verify_runtime_report_binding(result_path, report_path)

    assert binding["gate_passed"] is False
    assert binding["checks"]["processed_frame_count_matches"] is False


def test_sim_result_binds_bounded_prefix_and_runtime_input_hash(tmp_path: Path) -> None:
    report = RuntimeRunReport(
        descriptor_hash="c" * 64,
        input_hash="d" * 64,
        processed_frame_ids=("sim:demo:0", "sim:demo:1"),
        started_at_s=0.0,
        finished_at_s=0.2,
        stream_exhausted=False,
        max_frames=2,
    )
    report_path = write_runtime_run_report(tmp_path / "sim-runtime-report.json", report)
    result_path = _write_result(
        tmp_path / "sim-result.json",
        {
            "schema_version": 1,
            "event": "simulator_scenario_run_finished",
            "scenario_id": "demo",
            "scenario_manifest_hash": "e" * 64,
            "runtime_input_hash": "d" * 64,
            "runtime_descriptor_hash": "c" * 64,
            "requested_frames": 2,
            "completed_frames": 2,
            "terminal_reason": "frame_budget_complete",
            "truth_free": True,
            "closed": True,
        },
    )

    binding = verify_runtime_report_binding(result_path, report_path)

    assert binding["gate_passed"] is True
    assert binding["result"]["runtime_mode"] == "sim"
    assert binding["checks"]["termination_contract_matches"] is True
    assert binding["checks"]["processed_frame_ids_match"] is True
    assert binding["expectations"]["embedded_report_present"] is False


@pytest.mark.parametrize("mode", ("hil", "real"))
def test_receive_only_observation_result_binds_for_hil_and_real(tmp_path: Path, mode: str) -> None:
    result_path, report_path, report = _observation_fixture(tmp_path, mode)

    binding = verify_runtime_report_binding(
        result_path,
        report_path,
        expected_runtime_mode=mode,
    )

    assert binding["gate_passed"] is True
    assert binding["result"]["runtime_mode"] == mode
    assert binding["runtime_report"]["canonical_sha256"] == report.canonical_hash
    assert binding["checks"]["output_safety_contract"] is True
    assert binding["checks"]["termination_contract_matches"] is True
    assert binding["expectations"]["require_embedded_report"] is True


def test_receive_only_observation_rejects_transmit_counter(tmp_path: Path) -> None:
    result_path, report_path, _ = _observation_fixture(tmp_path, "hil")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["messages_transmitted"] = 1
    result_path.write_text(json.dumps(result), encoding="utf-8")

    binding = verify_runtime_report_binding(result_path, report_path)

    assert binding["gate_passed"] is False
    assert binding["checks"]["output_safety_contract"] is False
    assert binding["checks"]["result_gate_passed"] is False
    assert "output_safety_contract" in binding["reasons"]


def test_receive_only_observation_rejects_event_mode_drift(tmp_path: Path) -> None:
    result_path, report_path, _ = _observation_fixture(tmp_path, "hil")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["runtime_mode"] = "real"
    result_path.write_text(json.dumps(result), encoding="utf-8")

    binding = verify_runtime_report_binding(result_path, report_path)

    assert binding["gate_passed"] is False
    assert binding["checks"]["runtime_mode_matches"] is False


def test_receive_only_observation_requires_explicit_termination_contract(tmp_path: Path) -> None:
    result_path, report_path, _ = _observation_fixture(tmp_path, "real")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result.pop("max_frames")
    result_path.write_text(json.dumps(result), encoding="utf-8")

    binding = verify_runtime_report_binding(result_path, report_path)

    assert binding["gate_passed"] is False
    assert binding["checks"]["termination_contract_matches"] is False


def test_actual_simulator_result_exposes_bindable_runtime_input(tmp_path: Path) -> None:
    runner = SimulatorScenarioRunner(
        SIM_MANIFEST,
        config=SimulatorScenarioRunnerConfig(frames=2, dt_s=0.1, enable_ai=False),
    )
    result = runner.run()
    result_path = _write_result(tmp_path / "actual-sim-result.json", result.to_mapping_with_hash())
    report_path = result.write_runtime_run_report(tmp_path / "actual-sim-report.json")

    binding = verify_runtime_report_binding(result_path, report_path)

    assert binding["gate_passed"] is True
    assert binding["checks"]["input_hash_matches"] is True
    assert binding["checks"]["processed_frame_ids_match"] is True


def test_sim_bounded_prefix_fails_when_complete_is_required(tmp_path: Path) -> None:
    result_path, report_path, _ = _sim_fixture(tmp_path)
    binding = verify_runtime_report_binding(
        result_path,
        report_path,
        require_complete=True,
    )

    assert binding["gate_passed"] is False
    assert binding["checks"]["termination_contract_matches"] is False


def test_sim_binding_requires_requested_frame_budget(tmp_path: Path) -> None:
    result_path, report_path, _ = _sim_fixture(tmp_path)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result.pop("requested_frames")
    result_path.write_text(json.dumps(result), encoding="utf-8")

    binding = verify_runtime_report_binding(result_path, report_path)

    assert binding["gate_passed"] is False
    assert binding["checks"]["termination_contract_matches"] is False


def _sim_fixture(tmp_path: Path) -> tuple[Path, Path, RuntimeRunReport]:
    report = RuntimeRunReport(
        descriptor_hash="c" * 64,
        input_hash="d" * 64,
        processed_frame_ids=("sim:demo:0", "sim:demo:1"),
        started_at_s=0.0,
        finished_at_s=0.2,
        stream_exhausted=False,
        max_frames=2,
    )
    report_path = write_runtime_run_report(tmp_path / "runtime-report.json", report)
    result_path = _write_result(
        tmp_path / "sim-result.json",
        {
            "schema_version": 1,
            "event": "simulator_scenario_run_finished",
            "scenario_id": "demo",
            "scenario_manifest_hash": "e" * 64,
            "runtime_input_hash": "d" * 64,
            "runtime_descriptor_hash": "c" * 64,
            "requested_frames": 2,
            "completed_frames": 2,
            "terminal_reason": "frame_budget_complete",
            "truth_free": True,
            "closed": True,
        },
    )
    return result_path, report_path, report


def test_cli_writes_schema_valid_binding_report(tmp_path: Path, capsys) -> None:
    result_path, report_path, _ = _integrated_fixture(tmp_path)
    output_path = tmp_path / "binding.json"

    assert main([str(result_path), str(report_path), "--out", str(output_path)]) == 0
    document = json.loads(output_path.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = list(Draft202012Validator(schema).iter_errors(document))
    assert errors == []
    assert json.loads(capsys.readouterr().out)["gate_passed"] is True


def test_binding_rejects_duplicate_result_keys(tmp_path: Path) -> None:
    _, report_path, _ = _integrated_fixture(tmp_path)
    result_path = tmp_path / "duplicate.json"
    result_path.write_text(
        '{"event":"integrated_replay_finished","event":"integrated_replay_finished"}',
        encoding="utf-8",
    )

    assert main([str(result_path), str(report_path)]) == 1


def test_cli_failure_document_is_schema_valid(tmp_path: Path, capsys) -> None:
    _, report_path, _ = _integrated_fixture(tmp_path)
    result_path = tmp_path / "invalid.json"
    result_path.write_text('{"event":', encoding="utf-8")
    output_path = tmp_path / "binding-error.json"

    assert main([str(result_path), str(report_path), "--out", str(output_path)]) == 1
    document = json.loads(output_path.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = list(Draft202012Validator(schema).iter_errors(document))

    assert errors == []
    assert document["event"] == "runtime_report_binding_error"
    assert document["gate_passed"] is False
    assert document["result"] is None
    assert document["error"]["type"] == "RuntimeReportBindingError"
    assert set(document["checks"]) == {
        "result_json_valid",
        "runtime_report_valid",
        "result_event_supported",
        "result_gate_passed",
        "runtime_mode_matches",
        "descriptor_hash_matches",
        "input_hash_matches",
        "processed_frame_count_matches",
        "processed_frame_ids_match",
        "termination_contract_matches",
        "embedded_report_matches",
        "output_safety_contract",
        "sim_truth_free",
        "sim_closed",
    }
    assert json.loads(capsys.readouterr().out)["event"] == "runtime_report_binding_error"
