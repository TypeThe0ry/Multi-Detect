#!/usr/bin/env python3
"""Launch the installable ``polaris-sim-gateway`` entry point from this checkout."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from multidetect.polaris_sim.gateway.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
