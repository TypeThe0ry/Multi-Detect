from __future__ import annotations

import argparse
import json
from pathlib import Path

from multidetect.dataset_plan import build_dataset_plan


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build leak-resistant YOLO path lists from an audited source plan."
    )
    parser.add_argument("plan", type=Path)
    parser.add_argument("out_dir", type=Path)
    args = parser.parse_args()
    report = build_dataset_plan(args.plan, args.out_dir)
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
