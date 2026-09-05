"""Verify a hash-bound three-airframe SIM scenario-matrix artifact."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    _repository_source = Path(__file__).resolve().parents[1] / "src"
    if _repository_source.is_dir() and str(_repository_source) not in sys.path:
        sys.path.insert(0, str(_repository_source))

from multidetect.simulator.scenario_matrix import verify_file as verify_matrix_file


def verify_file(
    path: str | Path,
    *,
    expected_release_label: str | None = None,
) -> dict[str, Any]:
    """Verify one matrix file and attach its resolved path to the report."""

    source = Path(path).expanduser().resolve()
    result = verify_matrix_file(
        source,
        expected_release_label=expected_release_label,
        require_all_airframes=True,
    )
    raw = json.loads(source.read_text(encoding="utf-8-sig"))
    if isinstance(raw, dict):
        for field in (
            "scenario_count",
            "modes",
            "claims",
            "truth_loaded",
            "truth_free",
            "enable_ai",
            "arm_on_start",
            "repeatability_checked",
        ):
            result[field] = raw.get(field)
    result["evidence_path"] = str(source)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--expected-release-label")
    args = parser.parse_args(argv)
    try:
        result = verify_file(
            args.evidence,
            expected_release_label=args.expected_release_label,
        )
    except (
        OSError,
        UnicodeError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
        RuntimeError,
    ) as exc:
        result = {
            "event": "simulator_scenario_matrix_verified",
            "schema_version": 1,
            "gate_passed": False,
            "checks": {"readable_json": False},
            "reasons": [f"{type(exc).__name__}: {exc}"],
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("gate_passed") is True else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
