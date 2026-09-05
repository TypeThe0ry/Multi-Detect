"""Run the deterministic native-QGC interactive SIM acceptance fixture."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    _repository_source = Path(__file__).resolve().parents[1] / "src"
    if _repository_source.is_dir() and str(_repository_source) not in sys.path:
        sys.path.insert(0, str(_repository_source))

from multidetect.simulator.interactive_acceptance import main, run_acceptance

__all__ = ["main", "run_acceptance"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
