#!/usr/bin/env python3
"""Verify deterministic actuator safety evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    _repository_source = Path(__file__).resolve().parents[1] / "src"
    if _repository_source.is_dir() and str(_repository_source) not in sys.path:
        sys.path.insert(0, str(_repository_source))

from multidetect.actuator_safety import verify_actuator_safety_evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--expected-release-label")
    args = parser.parse_args()
    try:
        document = json.loads(args.evidence.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        result = {
            "event": "actuator_safety_software_evidence_verified",
            "gate_passed": False,
            "checks": {},
            "reasons": [f"unable to load evidence: {exc}"],
        }
    else:
        result = verify_actuator_safety_evidence(
            document,
            expected_release_label=args.expected_release_label,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
