from __future__ import annotations

import ast
from dataclasses import fields
from pathlib import Path

from multidetect.backends import RawSensorRuntimeBackend, ReplayBackend
from multidetect.replay import ReplayRecording
from multidetect.runtime import RuntimeBackend, RuntimeMode
from multidetect.simulation import ScenarioRuntimeInput, load_runtime_scenario

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "configs/scenarios/river_rescue_vtol_001.scenario.json"


def test_runtime_scenario_type_has_no_ground_truth_or_target_answer_field() -> None:
    runtime_input = load_runtime_scenario(SCENARIO, mode=RuntimeMode.SIM)
    field_names = {item.name for item in fields(ScenarioRuntimeInput)}
    serialized = runtime_input.canonical_json.lower()

    assert "evaluation_truth" not in field_names
    assert "ground_truth" not in field_names
    assert "targets" not in field_names
    assert "red_life_jacket" not in serialized
    assert "103.80118" not in serialized


def test_runtime_backend_protocol_and_replay_backend_do_not_expose_truth() -> None:
    protocol_names = set(vars(RuntimeBackend))
    backend = ReplayBackend(ReplayRecording("empty", (), "a" * 64))

    assert "truth" not in protocol_names
    assert "ground_truth" not in protocol_names
    assert {"descriptor", "clock", "input_hash", "observations", "close"}.issubset(protocol_names)
    assert not hasattr(backend, "truth")
    assert not hasattr(backend, "ground_truth")


def test_raw_sensor_backend_contract_cannot_supply_answers_or_detections() -> None:
    protocol_names = set(vars(RawSensorRuntimeBackend))
    forbidden = {
        "truth",
        "ground_truth",
        "evaluation_truth",
        "expectations",
        "expected_answer",
        "detector",
        "detections",
        "observations",
    }

    assert forbidden.isdisjoint(protocol_names)
    assert {"descriptor", "clock", "frame_source", "telemetry_provider"}.issubset(protocol_names)


def test_integrated_replay_launcher_has_no_ground_truth_or_expectations_import() -> None:
    source_path = ROOT / "src/multidetect/integrated_replay.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imported_modules.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )

    assert all("truth" not in module.lower() for module in imported_modules)
    assert all("expectation" not in module.lower() for module in imported_modules)
    assert all("simulation" not in module.lower().split(".") for module in imported_modules)
