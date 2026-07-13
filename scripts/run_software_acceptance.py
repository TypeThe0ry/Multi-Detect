from __future__ import annotations

import argparse
import json
from pathlib import Path

from multidetect.acceptance import run_software_acceptance


def main() -> int:
    parser = argparse.ArgumentParser(description="Run both software-only mission modes.")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    report = run_software_acceptance(args.project_root)
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
