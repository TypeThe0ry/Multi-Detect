from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    repository_source = Path(__file__).resolve().parents[1] / "src"
    if repository_source.is_dir() and str(repository_source) not in sys.path:
        sys.path.insert(0, str(repository_source))

from multidetect.field_link_address_state import main

if __name__ == "__main__":
    raise SystemExit(main())
