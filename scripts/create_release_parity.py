"""Generate ``configs/release-parity.json`` from explicit source contracts."""

from __future__ import annotations

# Make this wrapper runnable from a clean checkout, including ``python -S``.
# The package import remains normal when invoked through ``python -m`` or an
# installed console entry point.
import sys
from pathlib import Path

if __package__ in {None, ""}:
    _repository_source = Path(__file__).resolve().parents[1] / "src"
    if _repository_source.is_dir() and str(_repository_source) not in sys.path:
        sys.path.insert(0, str(_repository_source))

from multidetect.release_parity import (
    CONTRACT_GROUPS,
    DEFAULT_CONTRACT_FILES,
    DEFAULT_FILE_LIST,
    PARITY_FIELDS,
    PARITY_SCHEMA_VERSION,
    ReleaseParityError,
    build_parser,
    build_release_parity_document,
    compute_parity,
    hash_contract,
    load_file_list,
    main,
    normalize_file_list,
    normalize_relative_path,
    normalize_source_bytes,
    serialize_release_parity,
    write_release_parity,
)

__all__ = [
    "CONTRACT_GROUPS",
    "DEFAULT_CONTRACT_FILES",
    "DEFAULT_FILE_LIST",
    "PARITY_FIELDS",
    "PARITY_SCHEMA_VERSION",
    "ReleaseParityError",
    "build_parser",
    "build_release_parity_document",
    "compute_parity",
    "hash_contract",
    "load_file_list",
    "main",
    "normalize_file_list",
    "normalize_relative_path",
    "normalize_source_bytes",
    "serialize_release_parity",
    "write_release_parity",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
