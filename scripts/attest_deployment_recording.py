#!/usr/bin/env python3
"""Create or verify a physical-field recording source attestation."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    repository_source = Path(__file__).resolve().parents[1] / "src"
    if str(repository_source) not in sys.path:
        sys.path.insert(0, str(repository_source))

from multidetect.deployment_recording_attestation_cli import main

if __name__ == "__main__":
    raise SystemExit(main())
