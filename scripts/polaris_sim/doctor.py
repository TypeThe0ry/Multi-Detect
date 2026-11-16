#!/usr/bin/env python3
"""Run read-only Polaris gateway connectivity checks."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from multidetect.polaris_sim.gateway.doctor import doctor_checks, render_report  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--http-port", type=int, default=8780)
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv)
    try:
        request = urllib.request.Request(f"http://{args.host}:{args.http_port}/v1/doctor")
        with urllib.request.urlopen(request, timeout=2.0) as response:
            report = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        report = doctor_checks(host=args.host, http_port=args.http_port)
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json_output else render_report(report))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
