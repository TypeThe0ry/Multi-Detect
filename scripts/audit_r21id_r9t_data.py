#!/usr/bin/env python3
"""CPU-only r9t preflight from the checked-in r7/r8 audit evidence."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--episode-keys-file", type=Path, required=True)
    p.add_argument("--episode-key-root", type=Path, action="append", required=True)
    p.add_argument("--index", type=Path, required=True)
    p.add_argument("--campaign", type=Path, action="append", default=[])
    args = p.parse_args()
    keys = [line.strip() for line in args.episode_keys_file.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")]
    assert len(keys) == 33 and len(keys) == len(set(keys))
    assert all("w0013" not in key for key in keys)
    assert "gen_r21id_r8_w0002:0" not in keys
    index = json.loads(args.index.read_text(encoding="utf-8"))
    assert len(index["worlds"]) == 53
    evidence = {
        "person->jersey_barrier": {"r7_rows": 161, "r7_independent_rgb": 154, "r8_rows": 62, "r8_independent_rgb": 57},
        "suv->postbox": {"r7_rows": 42, "r7_independent_rgb": 42, "r8_rows": 3, "r8_independent_rgb": 2},
        "car->mailbox": {"r7_rows": 0, "r7_independent_rgb": 0, "r8_rows": 0, "r8_independent_rgb": 0},
    }
    directions = {
        name: {
            "rows": values["r7_rows"] + values["r8_rows"],
            "independent_rgb": values["r7_independent_rgb"] + values["r8_independent_rgb"],
            "source": "docs/r7-data-audit-20261105.md + docs/r8-data-audit-20261105.md",
        }
        for name, values in evidence.items()
    }
    print(json.dumps({"audited_keys": len(keys), "historical_roots": len(args.campaign), "directions": directions}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
