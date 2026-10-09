"""Audit runtime-wind binding for an external planner Mission report.

This is a read-only evidence check.  It proves that the non-zero wind values
declared by a SIM runtime reached the truth-free ContextSnapshot used for
planning and were retained in the hash-bound Mission evidence.  It does not
claim physical wind calibration, HIL/REAL behavior, or outdoor performance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.serialization import canonical_sha256, strict_json_loads  # noqa: E402
from multidetect.runtime import RuntimeMode  # noqa: E402
from multidetect.simulation import load_runtime_scenario  # noqa: E402


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _load_object(path: Path) -> dict[str, Any]:
    raw = strict_json_loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain one JSON object")
    return raw


def audit_report(report: dict[str, Any], runtime_input: Any) -> dict[str, Any]:
    if report.get("gate_passed") is not True:
        raise ValueError("completed external campaign report required")
    campaign = report.get("campaign", report)
    completed = campaign.get("completed") if isinstance(campaign, dict) else None
    if completed is None and isinstance(campaign, dict):
        verified = campaign.get("verified_sorties")
        run_directory = report.get("paths", {}).get("run_directory")
        if isinstance(verified, list) and isinstance(run_directory, str):
            completed = [
                {
                    "task_id": row.get("task_id"),
                    "evidence_path": str(
                        Path(run_directory) / "campaign-missions" / f"mission-{index:03d}.json"
                    ),
                }
                for index, row in enumerate(verified, 1)
                if isinstance(row, dict)
            ]
    if not isinstance(completed, list) or not completed:
        raise ValueError("campaign report has no completed sorties")
    runtime_hash = runtime_input.canonical_hash
    wind = runtime_input.world
    wind_north = _finite(wind.wind_north_mps, "wind_north_mps")
    wind_east = _finite(wind.wind_east_mps, "wind_east_mps")
    gust = _finite(wind.gust_mps, "gust_mps")
    if not any(abs(value) > 0.0 for value in (wind_north, wind_east, gust)):
        raise ValueError("runtime must declare a non-zero wind or gust for this audit")

    bound_sorties = []
    for index, entry in enumerate(completed, 1):
        if not isinstance(entry, dict):
            raise ValueError(f"completed sortie {index} is not an object")
        report_path = Path(entry["evidence_path"]).resolve()
        sortie = _load_object(report_path)
        scenario = sortie.get("scenario")
        if not isinstance(scenario, dict):
            raise ValueError(f"sortie {index} lacks scenario binding")
        if scenario.get("runtime_input_sha256") != runtime_hash:
            raise ValueError(f"sortie {index} runtime input hash mismatch")
        context_document = sortie.get("documents", {}).get("context_snapshot", {}).get("document")
        if not isinstance(context_document, dict):
            raise ValueError(f"sortie {index} lacks ContextSnapshot")
        context_north = _finite(context_document.get("wind_north_mps"), "context wind north")
        context_east = _finite(context_document.get("wind_east_mps"), "context wind east")
        if context_north != wind_north or context_east != wind_east:
            raise ValueError(f"sortie {index} ContextSnapshot wind mismatch")
        if canonical_sha256(context_document) != sortie["documents"]["context_snapshot"]["sha256"]:
            raise ValueError(f"sortie {index} ContextSnapshot hash mismatch")
        bound_sorties.append(
            {
                "task_id": entry.get("task_id"),
                "evidence_path": str(report_path),
                "runtime_input_sha256": runtime_hash,
                "context_snapshot_sha256": sortie["documents"]["context_snapshot"]["sha256"],
                "wind_north_mps": context_north,
                "wind_east_mps": context_east,
            }
        )
    return {
        "schema_version": 1,
        "audit": "external_planner_wind_binding",
        "runtime_input_sha256": runtime_hash,
        "configured_wind_mps": {
            "north": wind_north,
            "east": wind_east,
            "gust": gust,
        },
        "sorties": bound_sorties,
        "sortie_count": len(bound_sorties),
        "runtime_wind_reached_context": True,
        "planner_input_truth_free": True,
        "physical_wind_calibration_claimed": False,
        "hil_real_evidence": False,
        "outdoor_gnss_evidence": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--scenario-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise ValueError("output must be new")
    report = _load_object(args.report)
    runtime_input = load_runtime_scenario(args.scenario_manifest, mode=RuntimeMode.SIM)
    result = audit_report(report, runtime_input)
    raw = args.report.read_bytes()
    result["input"] = {
        "report": str(args.report.resolve()),
        "report_bytes": len(raw),
        "report_sha256": hashlib.sha256(raw).hexdigest(),
        "scenario_manifest": str(args.scenario_manifest.resolve()),
    }
    result["evaluator_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {"sortie_count": result["sortie_count"], "runtime_wind_reached_context": True}
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
