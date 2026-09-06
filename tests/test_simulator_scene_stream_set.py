from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from multidetect.simulator import (
    SimulatorScenarioRunner,
    SimulatorScenarioRunnerConfig,
    build_scene_stream_set_binding,
    verify_scene_stream_set_file,
)
from multidetect.simulator.scene_stream_set import (
    EVENT,
    SimulatorSceneStreamSetError,
    main,
    verify_binding,
)

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "configs" / "scenarios"
SCHEMA = ROOT / "configs" / "schemas" / "simulator-scene-stream-set.schema.json"
SCENARIO_FILES = (
    SCENARIOS / "coastal_patrol_fixed_wing_001.scenario.json",
    SCENARIOS / "urban_inspection_multirotor_001.scenario.json",
    SCENARIOS / "river_rescue_vtol_001.scenario.json",
)


def _write_stream(scenario: Path, output: Path, *, frames: int = 3) -> Path:
    stream = (
        SimulatorScenarioRunner(
            scenario,
            config=SimulatorScenarioRunnerConfig(
                frames=frames,
                dt_s=0.1,
                record_scene_stream=True,
            ),
        )
        .run()
        .scene_stream
    )
    assert stream is not None
    stream.write_json(output)
    return output


def _write_three_streams(tmp_path: Path) -> list[Path]:
    return [
        _write_stream(
            scenario,
            tmp_path / f"{scenario.stem}.scene-stream.json",
        )
        for scenario in SCENARIO_FILES
    ]


def test_scene_stream_set_binds_all_airframes_and_revalidates(tmp_path: Path) -> None:
    sources = _write_three_streams(tmp_path)
    binding = build_scene_stream_set_binding(
        sources,
        manifest_directory=tmp_path,
        expected_release_label="0.3.0-pre.8",
    )

    assert binding["event"] == EVENT
    assert binding["runtime_mode"] == "sim"
    assert binding["modes"] == ["fixed-wing", "multirotor", "vtol"]
    assert len(binding["evidence"]) == 3
    assert all(value is True for value in binding["checks"].values())
    assert binding["external_runtime_acceptance"] == "not_claimed"
    assert binding["visual_acceptance"] == "not_claimed"
    assert binding["hardware_acceptance"] == "not_claimed"

    report = verify_binding(
        binding,
        manifest_directory=tmp_path,
        expected_release_label="0.3.0-pre.8",
    )
    assert report["gate_passed"] is True
    assert report["modes"] == binding["modes"]
    assert report["checks"] == binding["checks"]

    output = tmp_path / "scene-stream-set.json"
    output.write_text(json.dumps(binding, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    persisted = verify_scene_stream_set_file(output, expected_release_label="0.3.0-pre.8")
    assert persisted["gate_passed"] is True
    assert persisted["event"] == "simulator_scene_stream_set_revalidated"


def test_scene_stream_set_binding_is_order_independent_and_deterministic(
    tmp_path: Path,
) -> None:
    sources = _write_three_streams(tmp_path)
    first = build_scene_stream_set_binding(
        sources,
        manifest_directory=tmp_path,
        expected_release_label="0.3.0-pre.8",
    )
    second = build_scene_stream_set_binding(
        list(reversed(sources)),
        manifest_directory=tmp_path,
        expected_release_label="0.3.0-pre.8",
    )

    first_payload = json.dumps(first, ensure_ascii=False, indent=2, sort_keys=True)
    second_payload = json.dumps(second, ensure_ascii=False, indent=2, sort_keys=True)
    assert first_payload == second_payload


def test_scene_stream_set_cli_binds_and_schema_validates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sources = _write_three_streams(tmp_path)
    output = tmp_path / "binding.json"
    assert (
        main(
            [
                "--bind",
                "--expected-release-label",
                "0.3.0-pre.8",
                "--manifest-directory",
                str(tmp_path),
                "--out",
                str(output),
                *(str(source) for source in sources),
            ]
        )
        == 0
    )
    printed = json.loads(capsys.readouterr().out)
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written == printed
    assert written["event"] == EVENT
    assert written["gate_passed"] is True

    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(written)


def test_scene_stream_set_rejects_duplicate_mode_and_tampering(tmp_path: Path) -> None:
    sources = _write_three_streams(tmp_path)
    duplicate = tmp_path / "duplicate-fixed-wing.scene-stream.json"
    shutil.copyfile(sources[0], duplicate)
    with pytest.raises(SimulatorSceneStreamSetError, match="scenario IDs must be unique"):
        build_scene_stream_set_binding(
            [sources[0], sources[1], duplicate],
            manifest_directory=tmp_path,
            expected_release_label="0.3.0-pre.8",
        )

    binding = build_scene_stream_set_binding(
        sources,
        manifest_directory=tmp_path,
        expected_release_label="0.3.0-pre.8",
    )
    raw = json.loads(json.dumps(binding))
    raw["evidence"][0]["snapshot_count"] += 1
    report = verify_binding(
        raw,
        manifest_directory=tmp_path,
        expected_release_label="0.3.0-pre.8",
    )
    assert report["gate_passed"] is False
    assert any("snapshot_count does not match source" in reason for reason in report["reasons"])

    source_raw = json.loads(sources[0].read_text(encoding="utf-8"))
    source_raw["snapshots"][0]["pose"]["north_m"] += 1.0
    # Leave the embedded frame hash stale: the independent stream verifier
    # must reject the source before the aggregate can be accepted.
    sources[0].write_text(json.dumps(source_raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tampered = verify_binding(
        binding,
        manifest_directory=tmp_path,
        expected_release_label="0.3.0-pre.8",
    )
    assert tampered["gate_passed"] is False
    assert any("verifier failed" in reason for reason in tampered["reasons"])


def test_scene_stream_set_rejects_release_mismatch_and_unknown_scenario(tmp_path: Path) -> None:
    sources = _write_three_streams(tmp_path)
    binding = build_scene_stream_set_binding(
        sources,
        manifest_directory=tmp_path,
        expected_release_label="0.3.0-pre.8",
    )
    mismatch = verify_binding(
        binding,
        manifest_directory=tmp_path,
        expected_release_label="0.3.0",
    )
    assert mismatch["gate_passed"] is False
    assert any("release_label" in reason for reason in mismatch["reasons"])

    unknown = json.loads(json.dumps(binding))
    unknown["evidence"][0]["scenario_id"] = "unknown"
    unknown_report = verify_binding(
        unknown,
        manifest_directory=tmp_path,
        expected_release_label="0.3.0-pre.8",
    )
    assert unknown_report["gate_passed"] is False
    assert any("scenario_id" in reason for reason in unknown_report["reasons"])

    release_tampered = json.loads(json.dumps(binding))
    release_tampered["release_label"] = "0.3.0"
    release_report = verify_binding(
        release_tampered,
        manifest_directory=tmp_path,
    )
    assert release_report["gate_passed"] is False
    assert any(
        "release_label does not match source" in reason for reason in release_report["reasons"]
    )

    path_tampered = json.loads(json.dumps(binding))
    path_tampered["evidence"][0]["path"] = str(sources[0])
    path_report = verify_binding(
        path_tampered,
        manifest_directory=tmp_path,
        expected_release_label="0.3.0-pre.8",
    )
    assert path_report["gate_passed"] is False
    assert any("path does not match source" in reason for reason in path_report["reasons"])


def test_scene_stream_set_direct_script_has_clean_help() -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "verify_simulator_scene_stream_set.py"),
            "--help",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert "--bind" in completed.stdout
