"""CLI entry point for the deterministic E7 release-gate manifest writer."""

from __future__ import annotations

# Keep the repository wrapper runnable from a clean checkout.  Most project
# commands use an installed editable package (or an explicit ``PYTHONPATH``),
# but a release operator commonly invokes this file directly with
# ``python scripts/create_release_gate_manifest.py``.  In that case Python's
# import path starts at ``scripts/`` and does not include the sibling ``src/``
# tree.  Add only this repository's source directory, and only for direct
# script execution; installed/namespace imports remain untouched.
import sys
from pathlib import Path

if __package__ in {None, ""}:
    _repository_root = Path(__file__).resolve().parents[1]
    if str(_repository_root) not in sys.path:
        sys.path.insert(0, str(_repository_root))
    _repository_source = _repository_root / "src"
    if _repository_source.is_dir() and str(_repository_source) not in sys.path:
        sys.path.insert(0, str(_repository_source))

from multidetect.release_gate_manifest import (
    MANIFEST_SCHEMA_VERSION,
    RELEASE_GATE_SCHEMA_VERSION,
    STATUS_VALUES,
    ReleaseGateManifestError,
    build_manifest,
    build_parser,
    build_release_gate_manifest,
    create_release_gate_manifest,
    default_mode_statuses,
    generate_release_gate_manifest,
    load_parity_hashes,
    main,
    sha256_file,
    write_release_gate_manifest,
)

__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "RELEASE_GATE_SCHEMA_VERSION",
    "ReleaseGateManifestError",
    "STATUS_VALUES",
    "build_parser",
    "build_manifest",
    "build_release_gate_manifest",
    "create_release_gate_manifest",
    "default_mode_statuses",
    "generate_release_gate_manifest",
    "load_parity_hashes",
    "main",
    "sha256_file",
    "write_release_gate_manifest",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
