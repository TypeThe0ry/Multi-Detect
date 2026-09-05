#!/usr/bin/env python3
"""Reopen and reproduce a three-airframe PX4 reported-position response matrix."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    _repository_root = Path(__file__).resolve().parents[1]
    if str(_repository_root) not in sys.path:
        sys.path.insert(0, str(_repository_root))

from scripts.run_px4_reported_position_response_matrix import (
    ROOT,
    SCHEMA,
    SCHEMA_VERSION,
    build_matrix,
)


def verify_file(path: str | Path, *, repository_root: str | Path = ROOT) -> dict[str, object]:
    root = Path(repository_root).expanduser().resolve()
    evidence_path = Path(path).expanduser().resolve()
    document = json.loads(evidence_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("matrix evidence must be a JSON object")
    entries = document.get("entries")
    if not isinstance(entries, list):
        raise ValueError("matrix entries must be a list")
    release_identity = document.get("release_identity")
    if not isinstance(release_identity, dict):
        raise ValueError("matrix release identity must be an object")
    source_paths = [root / str(entry.get("source_path", "")) for entry in entries]
    reproduced = build_matrix(
        source_paths,
        repository_root=root,
        release_parity_path=root / str(release_identity.get("path", "")),
    )
    checks = {
        "schema": document.get("schema") == SCHEMA,
        "schema_version": document.get("schema_version") == SCHEMA_VERSION,
        "original_gate_passed": document.get("gate_passed") is True,
        "release_label": document.get("release_label") == reproduced.get("release_label"),
        "release_identity": document.get("release_identity") == reproduced.get("release_identity"),
        "source_tools": document.get("source_tools") == reproduced.get("source_tools"),
        "entries": entries == reproduced.get("entries"),
        "shared_checks": document.get("shared_checks") == reproduced.get("shared_checks"),
        "matrix_sha256": document.get("matrix_sha256") == reproduced.get("matrix_sha256"),
        "reproduced_gate_passed": reproduced.get("gate_passed") is True,
        "claim_boundary": (
            document.get("truth_free") is True
            and document.get("hardware_acceptance") == "not_claimed"
            and document.get("hil_acceptance") == "not_claimed"
            and document.get("real_acceptance") == "not_claimed"
        ),
    }
    return {
        "schema": "multidetect.px4-reported-position-response-matrix-verification",
        "schema_version": 1,
        "evidence_path": str(evidence_path),
        "gate_passed": all(checks.values()),
        "checks": checks,
        "matrix_sha256": reproduced.get("matrix_sha256"),
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
            "schema": "multidetect.px4-reported-position-response-matrix-verification",
            "schema_version": 1,
            "gate_passed": False,
            "checks": {"readable_and_reproducible": False},
            "reasons": [f"{type(exc).__name__}: {exc}"],
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("gate_passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
