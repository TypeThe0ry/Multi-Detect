from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from multidetect.runtime import RuntimeMode, RuntimeRunReport
from multidetect.runtime_observation import (
    RuntimeObservationResult,
    RuntimeObservationResultError,
    load_runtime_observation_result,
    write_runtime_observation_pair,
    write_runtime_observation_result,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "configs" / "schemas" / "runtime-observation-result.schema.json"


def _report(mode: RuntimeMode, *, complete: bool = False) -> RuntimeRunReport:
    return RuntimeRunReport(
        descriptor_hash=("a" if mode is RuntimeMode.HIL else "b") * 64,
        input_hash=("c" if mode is RuntimeMode.HIL else "d") * 64,
        processed_frame_ids=(f"{mode.value}:frame-0001", f"{mode.value}:frame-0002"),
        started_at_s=10.0,
        finished_at_s=10.2,
        stream_exhausted=complete,
        max_frames=None if complete else 2,
    )


@pytest.mark.parametrize("mode", (RuntimeMode.HIL, RuntimeMode.REAL))
def test_observation_builder_derives_the_receive_only_contract(mode: RuntimeMode) -> None:
    result = RuntimeObservationResult(mode, _report(mode))
    document = result.to_mapping()

    assert document["event"] == f"{mode.value}_runtime_observation_finished"
    assert document["runtime_mode"] == mode.value
    assert document["runtime_descriptor_hash"] == result.runtime_run_report.descriptor_hash
    assert document["runtime_input_hash"] == result.runtime_run_report.input_hash
    assert document["processed_frame_count"] == 2
    assert document["processed_frame_ids"] == list(result.runtime_run_report.processed_frame_ids)
    assert document["messages_transmitted"] == 0
    assert document["flight_control_enabled"] is False
    assert document["hardware_outputs_present"] is False
    assert document["output_locked"] is True
    assert document["runtime_run_report"] == result.runtime_run_report.to_mapping_with_hash()


def test_observation_builder_round_trips_pair_and_optional_result_hash(tmp_path: Path) -> None:
    result = RuntimeObservationResult(RuntimeMode.HIL, _report(RuntimeMode.HIL))
    result_path, report_path = write_runtime_observation_pair(
        tmp_path / "hil-result.json",
        tmp_path / "hil-report.json",
        result,
        include_result_hash=True,
    )

    loaded = load_runtime_observation_result(result_path)
    assert loaded.mode is RuntimeMode.HIL
    assert loaded.runtime_run_report == result.runtime_run_report
    assert json.loads(result_path.read_text(encoding="utf-8"))["sha256"] == result.canonical_hash
    assert json.loads(report_path.read_text(encoding="utf-8")) == (
        result.runtime_run_report.to_mapping_with_hash()
    )


def test_observation_result_is_valid_against_its_schema(tmp_path: Path) -> None:
    result = RuntimeObservationResult(RuntimeMode.REAL, _report(RuntimeMode.REAL, complete=True))
    output = write_runtime_observation_result(tmp_path / "real-result.json", result)
    document = json.loads(output.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    Draft202012Validator(schema).validate(document)
    assert document["stream_exhausted"] is True
    assert document["max_frames"] is None


def test_observation_result_rejects_replay_mode() -> None:
    document = RuntimeObservationResult(RuntimeMode.HIL, _report(RuntimeMode.HIL)).to_mapping()
    document["runtime_mode"] = "replay"
    document["event"] = "replay_runtime_observation_finished"

    with pytest.raises(RuntimeObservationResultError, match="hil or real"):
        RuntimeObservationResult.from_mapping(document)


def test_observation_result_rejects_top_level_identity_tamper() -> None:
    result = RuntimeObservationResult(RuntimeMode.HIL, _report(RuntimeMode.HIL))
    document = result.to_mapping()
    document["runtime_input_hash"] = "f" * 64

    with pytest.raises(RuntimeObservationResultError, match="runtime_input_hash"):
        RuntimeObservationResult.from_mapping(document)


def test_observation_result_rejects_embedded_report_tamper() -> None:
    result = RuntimeObservationResult(RuntimeMode.REAL, _report(RuntimeMode.REAL))
    document = result.to_mapping()
    document["runtime_run_report"]["processed_frame_count"] = 99

    with pytest.raises(RuntimeObservationResultError, match="RuntimeRunReport"):
        RuntimeObservationResult.from_mapping(document)


@pytest.mark.parametrize(
    ("stream_exhausted", "max_frames"),
    ((True, 2), (False, None)),
)
def test_observation_builder_rejects_ambiguous_termination(
    stream_exhausted: bool, max_frames: int | None
) -> None:
    report = RuntimeRunReport(
        descriptor_hash="a" * 64,
        input_hash="b" * 64,
        processed_frame_ids=("hil:frame-0001",),
        started_at_s=0.0,
        finished_at_s=0.1,
        stream_exhausted=stream_exhausted,
        max_frames=max_frames,
    )
    with pytest.raises(ValueError, match="max_frames"):
        RuntimeObservationResult(RuntimeMode.HIL, report)
