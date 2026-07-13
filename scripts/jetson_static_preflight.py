from __future__ import annotations

import argparse
import json
from pathlib import Path

from multidetect.jetson_profile import jetson_static_preflight


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate a Jetson runtime profile without opening hardware or models."
    )
    parser.add_argument("mission", type=Path)
    parser.add_argument("environment", type=Path)
    parser.add_argument("--allow-placeholders", action="store_true")
    parser.add_argument("--verify-model-files", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    report = jetson_static_preflight(
        args.mission,
        args.environment,
        allow_placeholders=args.allow_placeholders,
        verify_model_files=args.verify_model_files,
    )
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
