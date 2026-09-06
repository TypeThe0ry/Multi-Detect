"""Canonical Multi-Detect product version access.

The repository-level ``configs/product-version.json`` manifest is the source
of truth for every product surface.  Installed distributions normally retain
the resolved PEP 440 version in package metadata, which is used only when the
repository manifest is not present.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

_NUMERIC_PATTERN = re.compile(r"^(?P<numeric>\d+\.\d+\.\d+)$")
_PRERELEASE_LABEL_PATTERN = re.compile(r"^(?P<numeric>\d+\.\d+\.\d+)-pre\.(?P<sequence>[1-9]\d*)$")
_PYTHON_PRERELEASE_PATTERN = re.compile(r"^(?P<numeric>\d+\.\d+\.\d+)rc(?P<sequence>[1-9]\d*)$")


@dataclass(frozen=True, slots=True)
class ProductVersion:
    """Validated versions used by release artifacts and package managers."""

    schema_version: int
    product: str
    release_label: str
    numeric: str
    python: str
    channel: str

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> ProductVersion:
        required = {
            "schema_version",
            "product",
            "release_label",
            "numeric",
            "python",
            "channel",
        }
        missing = sorted(required.difference(raw))
        extra = sorted(set(raw).difference(required))
        if missing or extra:
            raise ValueError(f"invalid product version keys: missing={missing!r}, extra={extra!r}")
        if type(raw["schema_version"]) is not int:
            raise ValueError("product version schema_version must be an integer")
        for field_name in ("product", "release_label", "numeric", "python", "channel"):
            if not isinstance(raw[field_name], str):
                raise ValueError(f"product version {field_name} must be a string")

        version = cls(
            schema_version=raw["schema_version"],
            product=raw["product"],
            release_label=raw["release_label"],
            numeric=raw["numeric"],
            python=raw["python"],
            channel=raw["channel"],
        )
        version.validate()
        return version

    def validate(self) -> None:
        if self.schema_version != 1:
            raise ValueError(f"unsupported product version schema: {self.schema_version}")
        if self.product != "Multi-Detect":
            raise ValueError(f"unexpected product name: {self.product!r}")
        if _NUMERIC_PATTERN.fullmatch(self.numeric) is None:
            raise ValueError(f"invalid numeric version: {self.numeric!r}")
        if self.channel not in {"prerelease", "release"}:
            raise ValueError(f"invalid release channel: {self.channel!r}")

        if self.channel == "release":
            if self.release_label != self.numeric or self.python != self.numeric:
                raise ValueError(
                    "release channel requires release_label and python to equal numeric"
                )
            return

        release_match = _PRERELEASE_LABEL_PATTERN.fullmatch(self.release_label)
        python_match = _PYTHON_PRERELEASE_PATTERN.fullmatch(self.python)
        if release_match is None:
            raise ValueError(f"invalid prerelease label: {self.release_label!r}")
        if python_match is None:
            raise ValueError(f"invalid Python prerelease version: {self.python!r}")
        if release_match.group("numeric") != self.numeric:
            raise ValueError("release_label numeric base does not match numeric")
        if python_match.group("numeric") != self.numeric:
            raise ValueError("python numeric base does not match numeric")
        if release_match.group("sequence") != python_match.group("sequence"):
            raise ValueError("release_label and python prerelease sequences do not match")


def repository_version_manifest() -> Path | None:
    """Return the repository manifest path, honoring an explicit test/build override."""

    override = os.environ.get("MULTIDETECT_PRODUCT_VERSION_MANIFEST")
    if override:
        return Path(override).expanduser().resolve()

    module_path = Path(__file__).resolve()
    for parent in module_path.parents:
        candidate = parent / "configs" / "product-version.json"
        if candidate.is_file():
            return candidate
    return None


def _version_from_distribution_metadata() -> ProductVersion:
    python_version = metadata.version("multi-detect")
    prerelease_match = _PYTHON_PRERELEASE_PATTERN.fullmatch(python_version)
    if prerelease_match is not None:
        numeric = prerelease_match.group("numeric")
        sequence = prerelease_match.group("sequence")
        return ProductVersion(
            schema_version=1,
            product="Multi-Detect",
            release_label=f"{numeric}-pre.{sequence}",
            numeric=numeric,
            python=python_version,
            channel="prerelease",
        )

    if _NUMERIC_PATTERN.fullmatch(python_version) is not None:
        return ProductVersion(
            schema_version=1,
            product="Multi-Detect",
            release_label=python_version,
            numeric=python_version,
            python=python_version,
            channel="release",
        )
    raise RuntimeError(f"unsupported installed Multi-Detect version: {python_version!r}")


def load_product_version(manifest_path: str | Path | None = None) -> ProductVersion:
    """Load and validate the canonical product version manifest."""

    path = (
        Path(manifest_path).resolve()
        if manifest_path is not None
        else repository_version_manifest()
    )
    if path is None:
        return _version_from_distribution_metadata()
    if not path.is_file():
        raise FileNotFoundError(f"product version manifest not found: {path}")

    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("product version manifest must contain a JSON object")
    return ProductVersion.from_mapping(raw)


PRODUCT_VERSION = load_product_version()
__version__ = PRODUCT_VERSION.python
