"""Generate deterministic v188 evidence for the runtime-observation CLI."""

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
from multidetect.runtime import RuntimeMode, RuntimeRunReport, write_runtime_run_report
from multidetect.runtime_observation import (
    load_runtime_observation_result,
)
from multidetect.runtime_observation import (
    main as observation_cli_main,
)
from multidetect.runtime_report_binding import verify_runtime_report_binding

ITERATION = "v188"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def _summary(path: Path) -> dict[str, object]:
    payload = path.read_bytes()
    return {"path": path.name, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}


def _report(mode: RuntimeMode) -> RuntimeRunReport:
    seed = {RuntimeMode.HIL: ("1", "2"), RuntimeMode.REAL: ("3", "4")}[mode]
    return RuntimeRunReport(
        descriptor_hash=seed[0] * 64,
        input_hash=seed[1] * 64,
        processed_frame_ids=(f"{mode.value}:cli:0001", f"{mode.value}:cli:0002"),
        started_at_s=30.0,
        finished_at_s=30.2,
        stream_exhausted=False,
        max_frames=2,
    )


def _schema_errors(path: Path, schema_path: Path) -> int:
    document = json.loads(path.read_text(encoding="utf-8"))
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return sum(1 for _ in Draft202012Validator(schema).iter_errors(document))


def build_evidence(output_directory: Path, schema_path: Path) -> dict[str, object]:
    output_directory = output_directory.expanduser().resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    pairs: dict[str, dict[str, object]] = {}
    for mode in (RuntimeMode.HIL, RuntimeMode.REAL):
        report = _report(mode)
        report_path = output_directory / f"{mode.value}-runtime-report-{ITERATION}.json"
        result_path = output_directory / f"{mode.value}-runtime-observation-{ITERATION}.json"
        write_runtime_run_report(report_path, report)
        exit_code = observation_cli_main(
            [
                str(report_path),
                "--runtime-mode",
                mode.value,
                "--out",
                str(result_path),
                "--include-hash",
            ]
        )
        if exit_code != 0:
            raise RuntimeError(f"{mode.value} CLI build failed")
        binding = verify_runtime_report_binding(
            result_path,
            report_path,
            expected_runtime_mode=mode.value,
        )
        if binding["gate_passed"] is not True:
            raise RuntimeError(f"{mode.value} binding failed: {binding['reasons']}")
        pairs[mode.value] = {
            "result": _summary(result_path),
            "runtime_report": _summary(report_path),
            "schema_errors": _schema_errors(result_path, schema_path),
            "binding_gate_passed": binding["gate_passed"],
            "round_trip": load_runtime_observation_result(result_path).runtime_run_report == report,
        }

    return {
        "iteration": ITERATION,
        "release_label": PRODUCT_VERSION.release_label,
        "cli": "multidetect-runtime-observation-build",
        "module": "src/multidetect/runtime_observation.py",
        "modes": [RuntimeMode.HIL.value, RuntimeMode.REAL.value],
        "pairs": pairs,
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
    passed = all(
        item["schema_errors"] == 0
        and item["binding_gate_passed"] is True
        and item["round_trip"] is True
        for item in summary["pairs"].values()
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
