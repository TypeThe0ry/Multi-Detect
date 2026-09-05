#!/usr/bin/env python3
"""Verify an authenticated alert-link bench report without network I/O."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    repository_source = Path(__file__).resolve().parents[1] / "src"
    if repository_source.is_dir() and str(repository_source) not in sys.path:
        sys.path.insert(0, str(repository_source))

from multidetect.alert_link_bench import verify_main

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(verify_main())
