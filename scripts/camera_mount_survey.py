"""Repository wrapper for the physical camera-mount survey CLI."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    source = Path(__file__).resolve().parents[1] / "src"
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from multidetect.camera_mount_survey import main

if __name__ == "__main__":
    raise SystemExit(main())
