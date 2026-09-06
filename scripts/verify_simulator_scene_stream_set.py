"""Verify or bind the three-airframe simulator scene-stream set."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"
if SOURCE.is_dir() and str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))


def _run() -> int:
    from multidetect.simulator.scene_stream_set import main

    return main()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_run())
