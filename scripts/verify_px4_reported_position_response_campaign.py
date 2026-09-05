#!/usr/bin/env python3
"""Reopen and reproduce a PX4 response repeatability and fault campaign."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    _repository_root = Path(__file__).resolve().parents[1]
    if str(_repository_root) not in sys.path:
        sys.path.insert(0, str(_repository_root))

from scripts.run_px4_reported_position_response_campaign import (
    ROOT,
    SCHEMA,
    SCHEMA_VERSION,
    build_campaign,
)


def verify_file(path: str | Path, *, repository_root: str | Path = ROOT) -> dict[str, object]:
    root = Path(repository_root).expanduser().resolve()
    evidence = Path(path).expanduser().resolve()
    document = json.loads(evidence.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("campaign evidence must be a JSON object")
    positives = document.get("positive_runs")
    faults = document.get("fault_runs")
    release_identity = document.get("release_identity")
    if not isinstance(positives, list) or not isinstance(faults, list):
        raise ValueError("campaign source lists are unavailable")
    if not isinstance(release_identity, dict):
        raise ValueError("campaign release identity is unavailable")
    reproduced = build_campaign(
        [root / str(row.get("source_path", "")) for row in positives],
        [root / str(row.get("source_path", "")) for row in faults],
        repository_root=root,
        release_parity_path=root / str(release_identity.get("path", "")),
    )
    checks = {
        "schema": document.get("schema") == SCHEMA,
        "schema_version": document.get("schema_version") == SCHEMA_VERSION,
        "original_gate_passed": document.get("gate_passed") is True,
        "release_identity": document.get("release_identity") == reproduced.get("release_identity"),
        "source_tools": document.get("source_tools") == reproduced.get("source_tools"),
        "positive_runs": positives == reproduced.get("positive_runs"),
        "fault_runs": faults == reproduced.get("fault_runs"),
        "statistics": document.get("statistics") == reproduced.get("statistics"),
        "shared_checks": document.get("shared_checks") == reproduced.get("shared_checks"),
        "campaign_sha256": document.get("campaign_sha256") == reproduced.get("campaign_sha256"),
        "reproduced_gate_passed": reproduced.get("gate_passed") is True,
        "claim_boundary": (
            document.get("truth_free") is True
            and document.get("hardware_acceptance") == "not_claimed"
            and document.get("hil_acceptance") == "not_claimed"
            and document.get("real_acceptance") == "not_claimed"
        ),
    }
    return {
        "schema": "multidetect.px4-reported-position-response-campaign-verification",
        "schema_version": 1,
        "evidence_path": str(evidence),
        "gate_passed": all(checks.values()),
        "checks": checks,
        "campaign_sha256": reproduced.get("campaign_sha256"),
        "reasons": [name for name, passed in checks.items() if not passed],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--repository-root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        result = verify_file(args.evidence, repository_root=args.repository_root)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        result = {
            "schema": "multidetect.px4-reported-position-response-campaign-verification",
            "schema_version": 1,
            "gate_passed": False,
            "checks": {"readable_and_reproducible": False},
            "reasons": [f"{type(exc).__name__}: {exc}"],
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("gate_passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
