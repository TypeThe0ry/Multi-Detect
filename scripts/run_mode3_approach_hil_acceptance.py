from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

from multidetect.approach_acceptance import run_mode3_approach_hil_acceptance
from multidetect.compat import UTC


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run arbitrary-target Mode-3 approach advice and fail-closed abort HIL."
    )
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    report = run_mode3_approach_hil_acceptance()
    report["generated_at_utc"] = datetime.now(UTC).isoformat()
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
