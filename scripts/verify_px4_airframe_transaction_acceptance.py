"""Verify a journal-bound three-airframe PX4 Mission acceptance artifact."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from multidetect.px4_mission_acceptance import verify_px4_mission_transaction_acceptance


def verify_file(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    document = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("acceptance evidence must be a JSON object")
    result = verify_px4_mission_transaction_acceptance(document)
    result["evidence_path"] = str(source)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args(argv)
    try:
        result = verify_file(args.evidence)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        result = {
            "schema": "multidetect.px4-airframe-mission-transaction-verification",
            "schema_version": 1,
            "gate_passed": False,
            "checks": {"readable_json": False},
            "entries": [],
            "errors": [f"{type(exc).__name__}: {exc}"],
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("gate_passed") is True else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
