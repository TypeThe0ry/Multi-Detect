#!/usr/bin/env python3
"""Small HTTP client for testing the Polaris AI gateway."""

from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ai_client_demo.py")
    parser.add_argument("--base-url", default="http://127.0.0.1:8790")
    parser.add_argument("--token")
    parser.add_argument("--slot", type=int, choices=(5, 6, 7), default=5)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--prompt", default="find a red vehicle")
    group.add_argument("--query-json", type=Path, help="file containing a Spica 1.0 TargetQuery")
    args = parser.parse_args(argv)
    body = (
        {"prompt": args.prompt}
        if args.query_json is None
        else {"query": json.loads(args.query_json.read_text(encoding="utf-8"))}
    )
    request = urllib.request.Request(
        f"{args.base_url.rstrip('/')}/v1/query_target",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Polaris-Slot": str(args.slot),
            **({"Authorization": f"Bearer {args.token}"} if args.token else {}),
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        print(json.dumps(json.loads(response.read()), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
