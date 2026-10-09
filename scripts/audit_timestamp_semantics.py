"""Audit explicit timestamp provenance in a Mission dynamics report."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.serialization import strict_json_loads  # noqa: E402
from multidetect.simulation.timestamp_contract import capture_timestamp_contract  # noqa: E402


def audit_file(path: Path, expected_sha256: str) -> dict:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:
        raise ValueError("source SHA256 mismatch")
    report = strict_json_loads(raw.decode("utf-8"))
    capture = report["flight"]["phase_dynamics_observations"]
    status = capture_timestamp_contract(capture)
    return {
        "source_path": str(path),
        "source_sha256": digest,
        "timestamp_contract": status,
        "source_labels_are_publication_times": False,
        "source_labels_are_physical_sample_times": False,
        "host_receipts_are_source_clock": False,
        "physical_synchrony_proven": False,
        "controller_acceptance_proven": False,
        "final_release_allowed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--source-sha256", required=True)
    args = parser.parse_args()
    try:
        result = audit_file(args.source, args.source_sha256)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        parser.exit(2, f"audit rejected: {exc}\n")
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
