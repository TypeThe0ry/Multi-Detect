"""Read the configured wind from the same bounded SDF used by a SIM world.

This is a configuration receipt, not a wind sensor or proof that aerodynamic
forces are enabled. It deliberately does not silently trust the parallel
runtime JSON's north/east names or rewrite historical input files.
"""

from __future__ import annotations

import hashlib
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .onboard_executed_trajectory import EvidenceBinding
from .serialization import (
    AutonomyContractError,
    canonical_sha256,
    require_real,
    strict_json_loads,
)

CONTRACT = "multidetect.configured-SIM-world-wind-v1"
MAXIMUM_SOURCE_BYTES = 1024 * 1024


def _read(root: Path, source: EvidenceBinding) -> bytes:
    if type(source) is not EvidenceBinding or source.json_pointer is not None:
        raise AutonomyContractError("whole-file EvidenceBinding required")
    if source.bytes > MAXIMUM_SOURCE_BYTES:
        raise AutonomyContractError("configured world input exceeds bounded source cap")
    path = root / source.path
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
        raise AutonomyContractError("configured world input must be a contained regular file")
    before = path.stat()
    with path.open("rb") as stream:
        raw = stream.read(source.bytes + 1)
    after = path.stat()

    def identity(stat):
        return stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino

    if (
        identity(before) != identity(after)
        or len(raw) != source.bytes
        or hashlib.sha256(raw).hexdigest() != source.sha256
    ):
        raise AutonomyContractError("configured world source identity/bytes/SHA differ")
    return raw


def _one(parent: ET.Element, tag: str) -> ET.Element:
    matches = parent.findall(tag)
    if len(matches) != 1:
        raise AutonomyContractError(f"exactly one {tag} element required")
    return matches[0]


class _NoDtdBuilder(ET.TreeBuilder):
    def doctype(self, name, public_id, system_id):
        # Parser callbacks see decoded XML, including UTF-16. An ASCII byte
        # scan alone cannot enforce the no-DTD rule for supported encodings.
        raise AutonomyContractError("SDF DTD/entity declarations are unsupported")


def _sdf_number(text: str | None) -> float:
    if (
        type(text) is not str
        or re.fullmatch(
            r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?",
            text.strip(" \t\r\n"),
        )
        is None
    ):
        raise ValueError("ASCII SDF numeric spelling required")
    return float(text)


@dataclass(frozen=True, slots=True)
class ConfiguredSimWorldWind:
    world_source: EvidenceBinding
    runtime_source: EvidenceBinding
    wind_east_mps: float
    wind_north_mps: float
    wind_up_mps: float
    runtime_wind_east_mps: float
    runtime_wind_north_mps: float

    def __post_init__(self):
        for name in ("world_source", "runtime_source"):
            if type(getattr(self, name)) is not EvidenceBinding:
                raise TypeError(f"{name} must be EvidenceBinding")
        for name in (
            "wind_east_mps",
            "wind_north_mps",
            "wind_up_mps",
            "runtime_wind_east_mps",
            "runtime_wind_north_mps",
        ):
            object.__setattr__(
                self, name, require_real(getattr(self, name), name, minimum=-100, maximum=100)
            )

    @property
    def runtime_direction_matches(self) -> bool:
        return (
            self.runtime_wind_east_mps == self.wind_east_mps
            and self.runtime_wind_north_mps == self.wind_north_mps
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "contract": CONTRACT,
            "runtime_kind": "SIM",
            "world_source": self.world_source.to_mapping(),
            "runtime_source": self.runtime_source.to_mapping(),
            "supported_world_frame": "ENU-with-zero-heading",
            "configured_wind_enu_mps": [self.wind_east_mps, self.wind_north_mps, self.wind_up_mps],
            "runtime_declared_wind_ne_mps": [
                self.runtime_wind_north_mps,
                self.runtime_wind_east_mps,
            ],
            "runtime_direction_matches": self.runtime_direction_matches,
            "actor_input_source": "source-bound-SDF-configuration-not-measurement",
            "claims": {
                "actual_wind_measured": False,
                "wind_forces_applied_verified": False,
                "runtime_file_rewritten": False,
                "execution_authority": False,
                "physical_calibration_claimed": False,
                "final_release_allowed": False,
            },
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


def qualify_configured_sim_world_wind(
    *,
    artifact_root: str | Path,
    world_source: EvidenceBinding,
    runtime_source: EvidenceBinding,
) -> ConfiguredSimWorldWind:
    """Use SDF east/north, expose disagreement, and refuse unsupported frames.

    The caller must bind these exact mounted assets to its current runtime.
    Absent wind, rotated headings, vertical wind and duplicate XML are not
    treated as calm or guessed transforms. Only this explicit SIM scope is
    supported; the receipt cannot qualify HIL/REAL wind.
    """
    root = Path(artifact_root).resolve(strict=True)
    if not root.is_dir():
        raise AutonomyContractError("existing artifact root directory required")
    raw = _read(root, world_source)
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise AutonomyContractError("SDF DTD/entity declarations are unsupported")
    try:
        sdf = ET.fromstring(raw, parser=ET.XMLParser(target=_NoDtdBuilder()))
    except ET.ParseError as exc:
        raise AutonomyContractError("malformed bounded SDF") from exc
    if sdf.tag != "sdf" or sdf.get("version") != "1.9":
        raise AutonomyContractError("only SDF 1.9 configured wind supported")
    world = _one(sdf, "world")
    spherical = _one(world, "spherical_coordinates")
    if _one(spherical, "surface_model").text != "EARTH_WGS84":
        raise AutonomyContractError("only EARTH_WGS84 configured world supported")
    if _one(spherical, "world_frame_orientation").text != "ENU":
        raise AutonomyContractError("only explicit ENU world wind supported")
    headings = spherical.findall("heading_deg")
    if len(headings) > 1:
        raise AutonomyContractError("duplicate world heading")
    try:
        heading = _sdf_number(headings[0].text) if headings else 0.0  # SDF 1.9 default
        vector_text = _one(_one(world, "wind"), "linear_velocity").text or ""
        vector = tuple(
            _sdf_number(v) for v in re.split(r"[ \t\r\n]+", vector_text.strip(" \t\r\n"))
        )
    except (TypeError, ValueError) as exc:
        raise AutonomyContractError("numeric configured heading/wind required") from exc
    if not math.isfinite(heading) or heading != 0.0:
        raise AutonomyContractError("nonzero or invalid world heading not supported")
    if len(vector) != 3 or any(not math.isfinite(v) or abs(v) > 100 for v in vector):
        raise AutonomyContractError("finite bounded three-axis configured wind required")
    if vector[2] != 0.0:
        raise AutonomyContractError("vertical configured wind not supported by shared planner")
    runtime = strict_json_loads(_read(root, runtime_source))
    if not isinstance(runtime, dict) or not isinstance(runtime.get("world"), dict):
        raise AutonomyContractError("runtime world object required")
    values = runtime["world"]
    result = ConfiguredSimWorldWind(
        world_source,
        runtime_source,
        vector[0],
        vector[1],
        vector[2],
        values.get("wind_east_mps"),
        values.get("wind_north_mps"),
    )
    # Re-read both originals, not just their names, after derivation.
    if _read(root, world_source) != raw:
        raise AutonomyContractError("configured world changed during qualification")
    _read(root, runtime_source)
    return result
