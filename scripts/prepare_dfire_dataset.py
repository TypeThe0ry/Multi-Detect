from __future__ import annotations

import argparse
import json
from pathlib import Path

from multidetect.dataset_prepare import prepare_dfire_archive


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Remap and repair the audited D-Fire ZIP for the local class contract."
    )
    parser.add_argument("archive", type=Path)
    parser.add_argument("out_dir", type=Path)
    parser.add_argument(
        "--extract-images",
        action="store_true",
        help="also materialize image files; omit for a labels-only preparation audit",
    )
    args = parser.parse_args()
    report = prepare_dfire_archive(
        args.archive,
        args.out_dir,
        extract_images=args.extract_images,
    )
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
