"""Generate deterministic v187 evidence for the typed HIL/REAL result builder.

This command exercises the producer-side ``RuntimeObservationResult`` API,
then reopens the generated result and ``RuntimeRunReport`` through the same
binding verifier used by the release evidence chain.  The inputs are software
fixtures: no Pixhawk, GNSS, hardware or flight acceptance is claimed.
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

from multidetect.product_version import PRODUCT_VERSION
from multidetect.runtime import RuntimeMode, RuntimeRunReport
from multidetect.runtime_observation import (
    RuntimeObservationResult,
    load_runtime_observation_result,
    write_runtime_observation_pair,
)
from multidetect.runtime_report_binding import verify_runtime_report_binding

ITERATION = "v187"


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


def _report(mode: RuntimeMode) -> RuntimeRunReport:
    seed = {
        RuntimeMode.HIL: ("a", "b"),
        RuntimeMode.REAL: ("c", "d"),
    }[mode]
    return RuntimeRunReport(
        descriptor_hash=seed[0] * 64,
        input_hash=seed[1] * 64,
        processed_frame_ids=(
            f"{mode.value}:builder:0001",
            f"{mode.value}:builder:0002",
        ),
        started_at_s=20.0,
        finished_at_s=20.2,
        stream_exhausted=False,
        max_frames=2,
    )


def _schema_errors(document: object, schema_path: Path) -> int:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return sum(1 for _ in Draft202012Validator(schema).iter_errors(document))


def build_evidence(output_directory: Path, schema_path: Path) -> dict[str, object]:
    output_directory = output_directory.expanduser().resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    pairs: dict[str, tuple[Path, Path]] = {}
    binding_reports: dict[str, dict[str, object]] = {}
    schema_error_counts: dict[str, int] = {}

    for mode in (RuntimeMode.HIL, RuntimeMode.REAL):
        result = RuntimeObservationResult(mode, _report(mode))
        result_path, report_path = write_runtime_observation_pair(
            output_directory / f"{mode.value}-runtime-observation-{ITERATION}.json",
            output_directory / f"{mode.value}-runtime-report-{ITERATION}.json",
            result,
            include_result_hash=True,
        )
        pairs[mode.value] = (result_path, report_path)
        reopened = load_runtime_observation_result(result_path)
        if reopened != result:
            raise RuntimeError(f"{mode.value} result did not round-trip")
        schema_error_counts[mode.value] = _schema_errors(
            json.loads(result_path.read_text(encoding="utf-8")),
            schema_path,
        )
        binding = verify_runtime_report_binding(
            result_path,
            report_path,
            expected_runtime_mode=mode.value,
        )
        if binding["gate_passed"] is not True:
            raise RuntimeError(f"{mode.value} binding failed: {binding['reasons']}")
        binding_reports[mode.value] = {
            "gate_passed": binding["gate_passed"],
            "checks": binding["checks"],
            "reasons": binding["reasons"],
        }

    hil_result_path, hil_report_path = pairs[RuntimeMode.HIL.value]
    negative = json.loads(hil_result_path.read_text(encoding="utf-8"))
    negative["runtime_input_hash"] = "e" * 64
    negative_path = output_directory / f"hil-runtime-observation-tampered-{ITERATION}.json"
    _write_json(negative_path, negative)
    negative_binding = verify_runtime_report_binding(
        negative_path,
        hil_report_path,
        expected_runtime_mode=RuntimeMode.HIL.value,
    )
    if negative_binding["gate_passed"] is not False:
        raise RuntimeError("tampered observation unexpectedly passed")

    return {
        "iteration": ITERATION,
        "release_label": PRODUCT_VERSION.release_label,
        "schema": "multidetect.runtime-observation-result",
        "schema_version": 1,
        "builder": "src/multidetect/runtime_observation.py",
        "pair_writer": "write_runtime_observation_pair",
        "verifier": "src/multidetect/runtime_report_binding.py",
        "modes": [RuntimeMode.HIL.value, RuntimeMode.REAL.value],
        "positive_pairs": {
            mode: {
                "result": _source_summary(result_path),
                "runtime_report": _source_summary(report_path),
                "schema_errors": schema_error_counts[mode],
                "binding": binding_reports[mode],
            }
            for mode, (result_path, report_path) in pairs.items()
        },
        "negative_tamper": {
            "result": _source_summary(negative_path),
            "gate_passed": negative_binding["gate_passed"],
            "failed_checks": negative_binding["reasons"],
            "schema_errors": _schema_errors(
                json.loads(negative_path.read_text(encoding="utf-8")),
                schema_path,
            ),
        },
        "claim_boundary": {
            "software_runtime_identity_only": True,
            "hardware_acceptance": "not_claimed",
            "hil_acceptance": "not_claimed",
            "real_acceptance": "not_claimed",
            "flight_acceptance": "not_claimed",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument(
        "--schema",
        type=Path,
        default=Path("configs/schemas/runtime-observation-result.schema.json"),
    )
    args = parser.parse_args()
    summary = build_evidence(args.output_directory, args.schema)
    _write_json(args.output_directory / f"summary-{ITERATION}.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    positive = all(
        item["binding"]["gate_passed"] is True and item["schema_errors"] == 0
        for item in summary["positive_pairs"].values()
    )
    negative = (
        summary["negative_tamper"]["gate_passed"] is False
        and summary["negative_tamper"]["schema_errors"] == 0
    )
    return 0 if positive and negative else 1


if __name__ == "__main__":
    raise SystemExit(main())
