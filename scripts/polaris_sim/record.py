#!/usr/bin/env python3
"""Start or stop a gateway-managed Polaris simulation recording."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "stop"))
    parser.add_argument("--gateway", default="http://192.168.1.138:8780")
    parser.add_argument("--token", default=os.environ.get("POLARIS_SIM_TOKEN"))
    parser.add_argument("--no-ai-events", action="store_true")
    args = parser.parse_args()
    endpoint = args.gateway.rstrip("/") + f"/v1/record/{args.action}"
    payload = {"include_ai_events": not args.no_ai_events} if args.action == "start" else {}
    headers = {"Content-Type": "application/json"}
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"
    request = Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"record {args.action} failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
