from __future__ import annotations

import argparse
import json
from pathlib import Path

from multidetect.dataset_audit import audit_yolo_dataset, audit_yolo_zip


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit YOLO labels, splits and source leakage.")
    parser.add_argument("source", type=Path)
    parser.add_argument("--class-count", type=int, default=2)
    parser.add_argument("--skip-hashes", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.source.suffix.lower() == ".zip":
        report = audit_yolo_zip(args.source, class_count=args.class_count)
    else:
        report = audit_yolo_dataset(
            args.source,
            class_count=args.class_count,
            hash_images=not args.skip_hashes,
        )
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0 if report["clean"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
