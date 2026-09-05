"""CLI wrapper for the machine-readable v0.3.0 release gate."""

from __future__ import annotations

# Make the repository wrapper usable from a clean checkout without requiring
# an editable install or a caller-managed PYTHONPATH.  This branch only runs
# for ``python scripts/check_release_gate.py``; importing the module through
# the package keeps the normal interpreter path unchanged.
import sys
from pathlib import Path

if __package__ in {None, ""}:
    _repository_root = Path(__file__).resolve().parents[1]
    if str(_repository_root) not in sys.path:
        sys.path.insert(0, str(_repository_root))
    _repository_source = _repository_root / "src"
    if _repository_source.is_dir() and str(_repository_source) not in sys.path:
        sys.path.insert(0, str(_repository_source))

from multidetect.release_gate import main

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
