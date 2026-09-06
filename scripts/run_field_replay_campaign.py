#!/usr/bin/env python3
"""Create, inspect and advance a deployment replay campaign."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    _source = Path(__file__).resolve().parents[1] / "src"
    if str(_source) not in sys.path:
        sys.path.insert(0, str(_source))

from multidetect.field_replay_campaign import main

if __name__ == "__main__":
    raise SystemExit(main())
