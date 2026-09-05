#!/usr/bin/env python3
"""Run the deterministic actuator direction/limit safety bench."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Keep direct checkout execution working on a fresh workstation.  The console
# script is available after ``pip install -e .``; this fallback also covers the
# repository's documented ``python scripts/...`` form.
if __package__ in {None, ""}:
    _repository_source = Path(__file__).resolve().parents[1] / "src"
    if _repository_source.is_dir() and str(_repository_source) not in sys.path:
        sys.path.insert(0, str(_repository_source))

from multidetect.actuator_safety import run_actuator_safety_software_acceptance


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Exercise bounded servo mapping, neutral cancel/lost-lock recovery and "
            "one-shot watchdog failsafe without opening a hardware transport."
        )
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="Optional JSON evidence path. Existing files are replaced atomically.",
    )
    args = parser.parse_args()

    # Keep the evidence byte-stable.  Wall-clock timestamps belong in the
    # surrounding run log, not in the hash-bound deterministic contract.
    report = run_actuator_safety_software_acceptance()
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.out.with_name(f".{args.out.name}.{os.getpid()}.tmp")
        temporary.write_text(encoded, encoding="utf-8", newline="\n")
        os.replace(temporary, args.out)
    print(encoded, end="")
    return 0 if report["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
