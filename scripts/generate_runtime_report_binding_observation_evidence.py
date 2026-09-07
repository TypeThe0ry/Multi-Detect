"""Generate deterministic v186 RuntimeReportBinding observation evidence.

The generated HIL/REAL files are contract fixtures for the software verifier.
They exercise the exact receive-only result shape that a future field capture
producer must emit; they do not claim a Pixhawk, GNSS, hardware or flight run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

if __package__ in {None, ""}:
    _repository_source = Path(__file__).resolve().parents[1] / "src"
    if _repository_source.is_dir() and str(_repository_source) not in sys.path:
        sys.path.insert(0, str(_repository_source))

from multidetect.runtime import RuntimeRunReport, write_runtime_run_report
from multidetect.runtime_report_binding_set import (
    DEFAULT_REQUIRED_MODES,
    verify_runtime_report_binding_set,
)

RELEASE_LABEL = "0.3.0-pre.8"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def _source_summary(path: Path) -> dict[str, object]:
    payload = path.read_bytes()
    return {
        "path": path.name,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _make_pair(output_directory: Path, mode: str) -> tuple[Path, Path]:
    descriptor_hash = {
        "replay": "a" * 64,
        "sim": "b" * 64,
        "hil": "c" * 64,
        "real": "d" * 64,
    }[mode]
    input_hash = {
        "replay": "e" * 64,
        "sim": "f" * 64,
        "hil": "1" * 64,
        "real": "2" * 64,
    }[mode]
    frame_ids = {
        "replay": ("frame-0001", "frame-0002"),
        "sim": ("sim:v186-observation:0", "sim:v186-observation:1"),
        "hil": ("hil:frame-0001", "hil:frame-0002"),
        "real": ("real:frame-0001", "real:frame-0002"),
    }[mode]
    report = RuntimeRunReport(
        descriptor_hash=descriptor_hash,
        input_hash=input_hash,
        processed_frame_ids=frame_ids,
        started_at_s=100.0,
        finished_at_s=100.2,
        stream_exhausted=mode == "replay",
        max_frames=None if mode == "replay" else 2,
    )
    report_path = output_directory / f"{mode}-runtime-report-v186.json"
    write_runtime_run_report(report_path, report)

    if mode == "replay":
        result: dict[str, object] = {
            "schema_version": 2,
            "event": "integrated_replay_qgc_finished",
            "runtime_mode": mode,
            "gate_passed": True,
            "recording_hash": input_hash,
            "runtime_descriptor_hash": descriptor_hash,
            "ground_truth_isolated": True,
            "result": {"processed_frames": 2},
            "runtime_run_report": report.to_mapping_with_hash(),
        }
    elif mode == "sim":
        result = {
            "schema_version": 1,
            "event": "simulator_scenario_run_finished",
            "scenario_id": "v186-observation",
            "scenario_manifest_hash": "3" * 64,
            "runtime_input_hash": input_hash,
            "runtime_descriptor_hash": descriptor_hash,
            "requested_frames": 2,
            "completed_frames": 2,
            "terminal_reason": "frame_budget_complete",
            "truth_free": True,
            "closed": True,
        }
    else:
        result = {
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
        }
    result_path = output_directory / f"{mode}-runtime-result-v186.json"
    _write_json(result_path, result)
    return result_path, report_path


def _schema_errors(document: object, schema_path: Path) -> int:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return sum(1 for _ in Draft202012Validator(schema).iter_errors(document))


def build_evidence(output_directory: Path, schema_path: Path) -> dict[str, object]:
    output_directory = output_directory.expanduser().resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    pairs = {mode: _make_pair(output_directory, mode) for mode in DEFAULT_REQUIRED_MODES}
    specifications = [
        {
            "runtime_mode": mode,
            "result_path": result_path.name,
            "runtime_report_path": report_path.name,
        }
        for mode, (result_path, report_path) in pairs.items()
    ]
    full = verify_runtime_report_binding_set(
        specifications,
        required_modes=DEFAULT_REQUIRED_MODES,
        base_directory=output_directory,
        require_embedded_modes=("replay", "hil", "real"),
    )
    subset = verify_runtime_report_binding_set(
        specifications[:2],
        required_modes=("replay", "sim"),
        base_directory=output_directory,
    )

    hil_result_path, hil_report_path = pairs["hil"]
    negative_result = json.loads(hil_result_path.read_text(encoding="utf-8"))
    negative_result["messages_transmitted"] = 1
    negative_result_path = output_directory / "hil-runtime-result-transmit-v186.json"
    _write_json(negative_result_path, negative_result)
    negative = verify_runtime_report_binding_set(
        [
            {
                "runtime_mode": "hil",
                "result_path": negative_result_path.name,
                "runtime_report_path": hil_report_path.name,
            }
        ],
        required_modes=("hil",),
        base_directory=output_directory,
    )

    full_path = output_directory / "runtime-report-binding-set-v186.json"
    subset_path = output_directory / "runtime-report-binding-set-default-v186.json"
    negative_path = output_directory / "runtime-report-binding-set-negative-v186.json"
    _write_json(full_path, full)
    _write_json(subset_path, subset)
    _write_json(negative_path, negative)
    return {
        "release_label": RELEASE_LABEL,
        "full": {
            "path": full_path.name,
            **_source_summary(full_path),
            "gate_passed": full["gate_passed"],
            "schema_errors": _schema_errors(full, schema_path),
            "covered_modes": full["covered_modes"],
        },
        "software_subset": {
            "path": subset_path.name,
            **_source_summary(subset_path),
            "gate_passed": subset["gate_passed"],
            "schema_errors": _schema_errors(subset, schema_path),
        },
        "negative_transmit": {
            "path": negative_path.name,
            **_source_summary(negative_path),
            "gate_passed": negative["gate_passed"],
            "schema_errors": _schema_errors(negative, schema_path),
            "expected_gate_passed": False,
            "failed_check": "output_safety_contract",
        },
        "source_pairs": {
            mode: {
                "result": _source_summary(result_path),
                "runtime_report": _source_summary(report_path),
            }
            for mode, (result_path, report_path) in pairs.items()
        },
        "claim_boundary": full["claim_boundary"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument(
        "--schema",
        type=Path,
        default=Path("configs/schemas/runtime-report-binding-set.schema.json"),
    )
    args = parser.parse_args()
    summary = build_evidence(args.output_directory, args.schema)
    _write_json(args.output_directory / "summary-v186.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary["full"]["gate_passed"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
