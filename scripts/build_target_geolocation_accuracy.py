#!/usr/bin/env python3
"""Join a verified target-event replay with field truth and write an accuracy report.

This is the field-facing companion to ``validate_target_geolocation_accuracy``.
The replay manifest supplies estimated coordinates, event IDs, target IDs and
GPS freshness; the independent survey table supplies only truth coordinates.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    repository_source = Path(__file__).resolve().parents[1] / "src"
    if repository_source.is_dir() and str(repository_source) not in sys.path:
        sys.path.insert(0, str(repository_source))

from multidetect.geolocation_survey import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
