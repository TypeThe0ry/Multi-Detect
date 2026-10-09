"""Quarantine rejected SIM reports without converting acceptance failure into reward."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.serialization import canonical_sha256, strict_json_loads  # noqa: E402
from scripts.audit_mission_clock_receipts import read_bound  # noqa: E402


def classify(report):
    if (
        not isinstance(report, dict)
        or report.get("schema") != "multidetect.px4-vtol-sitl-mission-acceptance"
        or report.get("gate_passed") is not False
        or not isinstance(report.get("failure"), str)
        or not report["failure"].strip()
    ):
        raise ValueError("explicit rejected SIM report and failure required")
    outcome = report.get("shared_outcome_shadow", {}).get("outcome")
    executable = report.get("documents", {}).get("executable_mission")
    bound = (
        isinstance(outcome, dict)
        and isinstance(executable, dict)
        and isinstance(executable.get("document"), dict)
        and canonical_sha256(executable["document"]) == executable.get("sha256")
        and outcome.get("mission_hash") == executable["sha256"]
    )
    status = outcome.get("status") if isinstance(outcome, dict) else None
    if not bound:
        classification = "acceptance_rejected_outcome_unbound_or_missing"
    elif status == "completed":
        classification = "acceptance_rejected_despite_completed_outcome_claim"
    elif status in ("failed", "timeout"):
        classification = "acceptance_rejected_with_unsuccessful_outcome_claim"
    else:
        classification = "acceptance_rejected_without_terminal_outcome_claim"
    return {
        "schema_version": 1,
        "record_kind": "quarantined_sim_acceptance_rejection",
        "classification": classification,
        "failure_text": report["failure"],
        "reported_outcome_status": status,
        "outcome_document_hash_bound": bool(bound),
        "reported_outcome_reason": outcome.get("reason") if isinstance(outcome, dict) else None,
        "labels": {"task_completed": None, "policy_reward": None},
        "eligible_uses": ["failure_triage", "attempt_inventory"],
        "failure_cause_proven": False,
        "raw_telemetry_replayed": False,
        "journal_revalidated": False,
        "training_eligible": False,
        "model_promotion_allowed": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mission", type=Path, required=True)
    parser.add_argument("--mission-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    raw = read_bound(args.mission, args.mission_sha256)
    result = classify(strict_json_loads(raw.decode("utf-8")))
    result["mission_report_sha256"] = args.mission_sha256
    result["evaluator_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
        stream.write("\n")
    print(result["classification"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
