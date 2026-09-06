#!/usr/bin/env python3
"""Repository-source entry point for the GR01 review trust-registry verifier."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"
if __package__ in {None, ""} and SOURCE.is_dir() and str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from multidetect.gr01_review_trust_registry import main  # noqa: E402, I001


if __name__ == "__main__":
    raise SystemExit(main())
