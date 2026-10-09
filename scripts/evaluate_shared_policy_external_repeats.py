"""Fail-closed evaluation of repeated parent/candidate external SIM evidence.

This evaluator checks whether a predeclared repeat campaign is complete and
whether its primary metric is actually present.  It never treats wall-clock
router duration as a performance metric.  Missing runs, identity drift,
failed cleanup, or missing metrics keep promotion false.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"evidence must be an object: {path}")
    return value


def _campaign_identity(evidence: dict[str, Any]) -> str:
    return _identity(evidence, "campaign_input", "sha256")


def _identity(evidence: dict[str, Any], section: str, field: str) -> str:
    record = evidence.get(section)
    value = record.get(field) if isinstance(record, dict) else None
    if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value):
        return value.lower()
    return ""


def _same_identity(runs: list[dict[str, Any]], section: str, field: str) -> bool:
    values = {_identity(run, section, field) for run in runs}
    return len(values) == 1 and "" not in values


def _metric(evidence: dict[str, Any], definition: dict[str, Any]) -> float | None:
    path = definition.get("path")
    if path != "campaign.verified_sorties":
        if path != "mission_completed.planning_energy.newly_consumed_wh":
            raise ValueError("unsupported primary metric path")
        mission_path = evidence.get("paths", {}).get("mission_evidence")
        if not isinstance(mission_path, str):
            return None
        mission = _load(Path(mission_path))
        completed = mission.get("completed")
        if not isinstance(completed, list) or not completed:
            return None
        values = []
        for item in completed:
            if not isinstance(item, dict) or not isinstance(item.get("evidence_path"), str):
                return None
            sortie = _load(Path(item["evidence_path"]))
            if sortie.get("planning_energy", {}).get("physical_calibration_claimed") is not False:
                return None
            window = sortie.get("mission_receive_window", {})
            if window.get("full_mission_energy_claimed") is not True:
                return None
            value = window.get("window_energy_wh")
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return None
            try:
                number = float(value)
            except OverflowError:
                return None
            if not math.isfinite(number) or number < 0:
                return None
            values.append(number)
        total = sum(values)
        return total if math.isfinite(total) else None
    sorties = evidence.get("campaign", {}).get("verified_sorties")
    if not isinstance(sorties, list) or not _task_order(evidence):
        return None
    return float(len(sorties))


def _task_order(evidence: dict[str, Any]) -> tuple[str, ...]:
    campaign = evidence.get("campaign")
    sorties = campaign.get("verified_sorties") if isinstance(campaign, dict) else None
    if not isinstance(sorties, list) or not sorties:
        return ()
    task_ids = []
    for item in sorties:
        value = item.get("task_id") if isinstance(item, dict) else None
        if not isinstance(value, str) or not value.strip() or value in task_ids:
            return ()
        task_ids.append(value)
    return tuple(task_ids)


def _task_set(evidence: dict[str, Any]) -> tuple[str, ...]:
    """Return the sorted task identity set for counterbalanced campaigns."""
    return tuple(sorted(set(_task_order(evidence))))


def evaluate(
    *,
    config: dict[str, Any],
    parent_paths: list[Path],
    candidate_paths: list[Path],
) -> dict[str, Any]:
    parent = [_load(path) for path in parent_paths]
    candidate = [_load(path) for path in candidate_paths]
    parent_hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in parent_paths]
    candidate_hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in candidate_paths]
    all_hashes = parent_hashes + candidate_hashes
    distinct_reports = len(set(all_hashes)) == len(all_hashes)
    minimum_parent = int(config["minimum_parent_runs"])
    minimum_candidate = int(config["minimum_candidate_runs"])
    all_runs = parent + candidate
    parent_campaigns = {_campaign_identity(run) for run in parent}
    candidate_campaigns = {_campaign_identity(run) for run in candidate}
    invariants = {
        "same_scenario_manifest": (
            _same_identity(all_runs, "scenario", "manifest_sha256")
        ),
        "same_stable_airframe_profile": (
            _same_identity(all_runs, "runtime_identity", "airframe_profile_sha256")
        ),
        "same_campaign_identity": (
            len(parent_campaigns) == 1
            and len(candidate_campaigns) == 1
            and parent_campaigns != candidate_campaigns
            and "" not in parent_campaigns
            and "" not in candidate_campaigns
        ),
        "same_task_order_for_metric": (
            bool(all_runs) and all(_task_order(run) for run in all_runs)
            and len({_task_order(run) for run in all_runs}) == 1
        ),
        "same_task_set_for_metric": (
            bool(all_runs) and all(_task_order(run) for run in all_runs)
            and len({_task_set(run) for run in all_runs}) == 1
        ),
        "same_px4_mission_path": all(
            run.get("router", {})
            .get("result", {})
            .get("evaluator_bounded_mission_transaction_allowed")
            is True
            for run in all_runs
        ),
        "all_gate_passed": all(run.get("gate_passed") is True for run in all_runs),
        "all_cleanup_clean": all(run.get("cleanup_failures") == [] for run in all_runs),
    }
    metric_definition = config["primary_metric"]
    parent_metrics = [_metric(run, metric_definition) for run in parent]
    candidate_metrics = [_metric(run, metric_definition) for run in candidate]
    metric_present = all(value is not None for value in parent_metrics + candidate_metrics)
    counts_ready = (
        distinct_reports
        and len(parent) >= minimum_parent
        and len(candidate) >= minimum_candidate
    )
    required_invariants = config.get("required_invariants", list(invariants))
    invariants_ready = all(invariants.get(name) is True for name in required_invariants)
    metric_ready = bool(
        metric_present
        and bool(parent_metrics)
        and bool(candidate_metrics)
        and not str(config.get("metric_status", "")).startswith("quarantined")
    )
    parent_mean = sum(parent_metrics) / len(parent_metrics) if metric_ready else None
    candidate_mean = sum(candidate_metrics) / len(candidate_metrics) if metric_ready else None
    direction = metric_definition["direction"]
    margin = float(metric_definition["minimum_candidate_margin"])
    if metric_ready and direction == "maximize":
        gain_proven = candidate_mean >= parent_mean + margin
    elif metric_ready and direction == "minimize":
        gain_proven = candidate_mean <= parent_mean - margin
    else:
        gain_proven = False
    promotion_ready = counts_ready and invariants_ready and metric_ready and gain_proven
    return {
        "schema_version": 1,
        "evaluation_id": config["evaluation_id"],
        "product_version": config["product_version"],
        "runtime_mode": config["runtime_mode"],
        "config_sha256": hashlib.sha256(
            json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "parent_runs": [str(path) for path in parent_paths],
        "candidate_runs": [str(path) for path in candidate_paths],
        "parent_run_file_sha256": parent_hashes,
        "candidate_run_file_sha256": candidate_hashes,
        "distinct_run_reports": distinct_reports,
        "unique_report_counts": {
            "parent": len(set(parent_hashes)),
            "candidate": len(set(candidate_hashes)),
            "combined": len(set(all_hashes)),
        },
        "run_counts": {"parent": len(parent), "candidate": len(candidate)},
        "minimum_run_counts": {"parent": minimum_parent, "candidate": minimum_candidate},
        "invariants": invariants,
        "required_invariants": required_invariants,
        "primary_metric": {
            **metric_definition,
            "parent_values": parent_metrics,
            "candidate_values": candidate_metrics,
            "parent_mean": parent_mean,
            "candidate_mean": candidate_mean,
            "present_in_every_run": metric_present,
            "gain_proven": gain_proven,
        },
        "minimum_run_counts_met": counts_ready,
        "invariants_met": invariants_ready,
        "metric_ready": metric_ready,
        "independent_candidate_gain_proven": gain_proven,
        "candidate_promoted": False,
        "promotion_ready": promotion_ready,
        "learned_weights_loaded": False,
        "hil_real_evidence": False,
        "outdoor_gnss_evidence": False,
        "final_release_allowed": False,
        "notes": [
            (
                "Router wall-clock duration is intentionally not used as the primary "
                "performance metric."
            ),
            (
                "The evaluator stays fail-closed when the declared metric is missing "
                "or repeat counts are below policy."
            ),
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--parent", type=Path, action="append", required=True)
    parser.add_argument("--candidate", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("output must be new")
    result = evaluate(
        config=_load(args.config),
        parent_paths=args.parent,
        candidate_paths=args.candidate,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "promotion_ready": result["promotion_ready"],
                "candidate_promoted": result["candidate_promoted"],
            }
        )
    )
    return 0 if result["promotion_ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
