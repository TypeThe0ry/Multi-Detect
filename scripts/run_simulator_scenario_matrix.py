"""Run all repository-owned deterministic SIM scenario manifests."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    _repository_source = Path(__file__).resolve().parents[1] / "src"
    if _repository_source.is_dir() and str(_repository_source) not in sys.path:
        sys.path.insert(0, str(_repository_source))

from multidetect.simulator.scenario_matrix import main, run_scenario_matrix

__all__ = ["main", "run_scenario_matrix"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
