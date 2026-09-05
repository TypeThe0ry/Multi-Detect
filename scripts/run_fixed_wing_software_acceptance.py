#!/usr/bin/env python3
"""Run the deterministic fixed-wing software acceptance gate."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in {None, ""}:
    _repository_source = Path(__file__).resolve().parents[1] / "src"
    if _repository_source.is_dir() and str(_repository_source) not in sys.path:
        sys.path.insert(0, str(_repository_source))

from multidetect.fixed_wing_acceptance import run_fixed_wing_software_acceptance


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Exercise DET/TRK/LCK/TGT, bounded fixed-wing attitude control, "
            "RC cancellation and loss recovery without hardware."
        )
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="Optional JSON evidence path. Existing files are replaced atomically.",
    )
    args = parser.parse_args()

    report = run_fixed_wing_software_acceptance()
    report["generated_at_utc"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.out.with_name(f".{args.out.name}.{os.getpid()}.tmp")
        temporary.write_text(encoded, encoding="utf-8", newline="\n")
        os.replace(temporary, args.out)
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
