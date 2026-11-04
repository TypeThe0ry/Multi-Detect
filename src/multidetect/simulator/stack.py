"""Repository-owned PX4/Gazebo stack planning.

This module is intentionally a *planner*, not another process orchestrator.
It resolves a SIM runtime manifest to the checked-in SDF world/model assets,
validates every boundary that could accidentally reach a REAL vehicle, hashes
the inputs, and emits launch commands/environment suitable for a caller-owned
subprocess launcher.  No process, socket, or network connection is opened.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlparse

from ..autonomy.serialization import canonical_sha256
from ..runtime import RuntimeMode
from ..simulation import ScenarioRuntimeInput, load_runtime_scenario
from ..terrain_dem import TerrainDemError, load_local_dem_grid

SCHEMA_VERSION = 1
PROTECTED_GROUND_STATION_PORT = 14550
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})
DEFAULT_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PX4_CONTROL_PORT = 14540
DEFAULT_PX4_TELEMETRY_PORT = 14541
DEFAULT_QGC_OUTPUT_PORT = 14649
DEFAULT_QGC_INPUT_PORT = 14650
DEFAULT_METADATA_PORT = 14640
DEFAULT_RTSP_PORT = 8554

_BUILTIN_WORLD_RE = re.compile(r"^builtin://gazebo/(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*)(?:\.sdf)?$")
_BUILTIN_ASSET_RE = re.compile(
    r"^builtin://(?P<kind>maps|terrain)/(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*)$"
)
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SAFE_COMMAND_RE = re.compile(r"^[A-Za-z0-9_.+-]+(?:\.exe)?$")
_SAFE_ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_NETWORK_URI_RE = re.compile(r"(?:https?|ftp|fuel)://", re.IGNORECASE)


class SimulatorStackError(ValueError):
    """Raised when a repository-owned PX4/Gazebo stack plan is invalid."""


SimulatorStackIsolationError = SimulatorStackError
# Public short name used by the simulator CLI/API contract.
StackPlanError = SimulatorStackError


def airframe_profile_hash(document: Mapping[str, object]) -> str:
    """Hash stable aircraft identity, excluding per-run stack wiring.

    ``stack_plan_sha256`` records a concrete launch instance (ports, paths and
    process wiring). It must remain in evidence, but cannot be part of the
    policy's reusable airframe identity because those values intentionally
    change between runs.
    """
    if not isinstance(document, Mapping):
        raise TypeError("airframe profile document must be a mapping")
    identity = dict(document)
    identity.pop("stack_plan_sha256", None)
    return canonical_sha256(identity)


@dataclass(frozen=True, slots=True)
class BuiltinWorldReference:
    """A parsed ``builtin://gazebo`` URI and its local SDF path."""

    uri: str
    name: str
    path: Path

    @property
    def world_name(self) -> str:
        return self.name

    def to_mapping(self, *, repository_root: Path | None = None) -> dict[str, object]:
        return {
            "uri": self.uri,
            "name": self.name,
            "path": _display_path(self.path, repository_root),
        }


@dataclass(frozen=True, slots=True)
class BuiltinAssetReference:
    """A parsed repository-owned map or terrain URI and local path."""

    uri: str
    kind: str
    name: str
    path: Path

    def to_mapping(self, *, repository_root: Path | None = None) -> dict[str, object]:
        return {
            "uri": self.uri,
            "kind": self.kind,
            "name": self.name,
            "path": _display_path(self.path, repository_root),
        }


@dataclass(frozen=True, slots=True)
class SimulatorStackAsset:
    """One immutable repository asset and its content digest."""

    role: str
    path: Path
    sha256: str
    size_bytes: int

    def to_mapping(self, *, repository_root: Path | None = None) -> dict[str, object]:
        return {
            "role": self.role,
            "path": _display_path(self.path, repository_root),
            "absolute_path": str(self.path),
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class SimulatorModelAssetBundle:
    """Every repository-owned file exposed through the Gazebo model mount."""

    root: Path
    files: tuple[SimulatorStackAsset, ...]
    aggregate_sha256: str

    @property
    def file_count(self) -> int:
        return len(self.files)

    def portable_mapping(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "file_count": self.file_count,
            "files": [
                {
                    "role": item.role,
                    "path": _display_path(item.path, self.root),
                    "sha256": item.sha256,
                    "size_bytes": item.size_bytes,
                }
                for item in self.files
            ],
            "assets_sha256": self.aggregate_sha256,
        }

    def to_mapping(self) -> dict[str, object]:
        return {
            "root": str(self.root),
            **self.portable_mapping(),
        }


@dataclass(frozen=True, slots=True)
class SimulatorStackPorts:
    """Independent literal-loopback ports for one disposable SIM job."""

    host: str = "127.0.0.1"
    px4_control_port: int = DEFAULT_PX4_CONTROL_PORT
    px4_telemetry_port: int = DEFAULT_PX4_TELEMETRY_PORT
    qgc_output_port: int = DEFAULT_QGC_OUTPUT_PORT
    qgc_input_port: int = DEFAULT_QGC_INPUT_PORT
    metadata_port: int = DEFAULT_METADATA_PORT
    rtsp_port: int = DEFAULT_RTSP_PORT

    def __post_init__(self) -> None:
        _validate_loopback_host(self.host)
        values = (
            ("px4_control_port", self.px4_control_port),
            ("px4_telemetry_port", self.px4_telemetry_port),
            ("qgc_output_port", self.qgc_output_port),
            ("qgc_input_port", self.qgc_input_port),
            ("metadata_port", self.metadata_port),
            ("rtsp_port", self.rtsp_port),
        )
        seen: dict[int, str] = {}
        for name, value in values:
            port = _validate_port(value, name)
            if port == PROTECTED_GROUND_STATION_PORT:
                raise SimulatorStackError(
                    f"{name} cannot use protected UDP {PROTECTED_GROUND_STATION_PORT}"
                )
            if port in seen:
                raise SimulatorStackError(
                    f"SIM ports must be distinct: {name} duplicates {seen[port]} ({port})"
                )
            seen[port] = name

    @property
    def all_ports(self) -> tuple[int, ...]:
        return (
            self.px4_control_port,
            self.px4_telemetry_port,
            self.qgc_output_port,
            self.qgc_input_port,
            self.metadata_port,
            self.rtsp_port,
        )

    @property
    def px4_control_endpoint(self) -> str:
        return f"udpout:{_endpoint_host(self.host)}:{self.px4_control_port}"

    @property
    def px4_telemetry_endpoint(self) -> str:
        return f"udp://{_endpoint_host(self.host)}:{self.px4_telemetry_port}"

    def to_mapping(self) -> dict[str, object]:
        return {
            "host": self.host,
            "px4_control_port": self.px4_control_port,
            "px4_telemetry_port": self.px4_telemetry_port,
            "qgc_output_port": self.qgc_output_port,
            "qgc_input_port": self.qgc_input_port,
            "metadata_port": self.metadata_port,
            "rtsp_port": self.rtsp_port,
            "all_ports": list(self.all_ports),
            "protected_port": PROTECTED_GROUND_STATION_PORT,
            "loopback_only": True,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> SimulatorStackPorts:
        if not isinstance(raw, Mapping):
            raise TypeError("ports must be a mapping")
        allowed = {
            "host",
            "px4_control_port",
            "px4_telemetry_port",
            "qgc_output_port",
            "qgc_input_port",
            "metadata_port",
            "rtsp_port",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise SimulatorStackError(
                f"ports has unknown field(s): {', '.join(sorted(map(str, unknown)))}"
            )
        return cls(**{str(key): value for key, value in raw.items()})  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class SimulatorStackAssets:
    """Resolved world/map/terrain/model assets and aggregate provenance."""

    repository_root: Path
    world: SimulatorStackAsset
    map: SimulatorStackAsset
    dem: SimulatorStackAsset
    model: SimulatorStackAsset
    model_config: SimulatorStackAsset
    model_root: SimulatorModelAssetBundle
    aggregate_sha256: str

    @property
    def world_path(self) -> Path:
        return self.world.path

    @property
    def model_path(self) -> Path:
        return self.model.path

    @property
    def map_path(self) -> Path:
        return self.map.path

    @property
    def dem_path(self) -> Path:
        return self.dem.path

    @property
    def model_config_path(self) -> Path:
        return self.model_config.path

    @property
    def asset_sha256(self) -> dict[str, str]:
        return {
            "world": self.world.sha256,
            "map": self.map.sha256,
            "dem": self.dem.sha256,
            "model": self.model.sha256,
            "model_config": self.model_config.sha256,
            "model_root": self.model_root.aggregate_sha256,
            "aggregate": self.aggregate_sha256,
        }

    @property
    def assets_sha256(self) -> str:
        return self.aggregate_sha256

    def to_mapping(self) -> dict[str, object]:
        files = [
            self.world.to_mapping(repository_root=self.repository_root),
            self.map.to_mapping(repository_root=self.repository_root),
            self.dem.to_mapping(repository_root=self.repository_root),
            self.model.to_mapping(repository_root=self.repository_root),
            self.model_config.to_mapping(repository_root=self.repository_root),
        ]
        return {
            "repository_root": str(self.repository_root),
            "files": files,
            "world": files[0],
            "map": files[1],
            "dem": files[2],
            "model": files[3],
            "model_config": files[4],
            "model_root": self.model_root.to_mapping(),
            "asset_sha256": self.asset_sha256,
            "assets_sha256": self.aggregate_sha256,
        }


@dataclass(frozen=True, slots=True)
class SimulatorStackPlan:
    """Hash-bound, serializable PX4/Gazebo launch plan."""

    scenario_id: str
    scenario_version: int
    scenario_manifest_hash: str
    seed: int
    vehicle_class: str
    vehicle_model: str
    airframe_profile_id: str
    gazebo_flavor: str
    gazebo_executable: str
    px4_executable: str
    world_uri: str
    world_name: str
    world_path: Path
    map_uri: str
    map_path: Path
    dem_uri: str
    dem_path: Path
    model_name: str
    model_path: Path
    repository_root: Path
    assets: SimulatorStackAssets
    ports: SimulatorStackPorts
    gazebo_command: tuple[str, ...]
    px4_command: tuple[str, ...]
    environment: tuple[tuple[str, str], ...] | Mapping[str, str]
    headless: bool = True
    realtime: bool = True
    # Scenario manifests use the canonical telemetry port so their hashes stay
    # portable.  A launcher may bind a different per-job port; retain the
    # explicit source-to-bound mapping in the plan so that remapping is visible
    # and included in the dynamic plan hash.
    sensor_port_bindings: tuple[tuple[str, int, int], ...] = ()
    schema_version: int = SCHEMA_VERSION
    # Tool discovery is advisory: a plan remains serializable in dry-run mode
    # when PX4/Gazebo are not installed on the planning host.
    tool_capabilities: tuple[tuple[str, bool], ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise SimulatorStackError(
                f"unsupported stack schema_version {self.schema_version}; expected {SCHEMA_VERSION}"
            )
        for value in (self.scenario_id, self.vehicle_model, self.model_name):
            if not isinstance(value, str) or _SAFE_IDENTIFIER_RE.fullmatch(value) is None:
                raise SimulatorStackError("stack identifiers contain unsupported characters")
        _validate_sha(self.scenario_manifest_hash, "scenario_manifest_hash")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            raise SimulatorStackError("seed must be a non-negative integer")
        if self.vehicle_class not in {"vtol", "fixed-wing", "multirotor"}:
            raise SimulatorStackError("vehicle_class must be 'vtol', 'fixed-wing' or 'multirotor'")
        if self.gazebo_flavor not in {"gz", "gazebo", "ign"}:
            raise SimulatorStackError("gazebo_flavor must be gz, gazebo or ign")
        if not isinstance(self.gazebo_command, Sequence) or not self.gazebo_command:
            raise SimulatorStackError("gazebo_command must be non-empty")
        if not isinstance(self.px4_command, Sequence) or not self.px4_command:
            raise SimulatorStackError("px4_command must be non-empty")
        if isinstance(self.environment, Mapping):
            items = tuple(sorted((str(k), str(v)) for k, v in self.environment.items()))
            object.__setattr__(self, "environment", items)
        if not isinstance(self.environment, Sequence):
            raise SimulatorStackError("environment must be key/value pairs")
        capabilities: dict[str, bool] = {}
        for item in self.tool_capabilities:
            if not isinstance(item, Sequence) or len(item) != 2:
                raise SimulatorStackError("tool_capabilities entries must be [name, available]")
            name, available = item
            if name not in {"gazebo", "px4"} or not isinstance(available, bool):
                raise SimulatorStackError("tool_capabilities must contain gazebo/px4 booleans")
            if name in capabilities:
                raise SimulatorStackError("tool_capabilities contains duplicate names")
            capabilities[name] = available
        object.__setattr__(self, "tool_capabilities", tuple(sorted(capabilities.items())))
        keys: set[str] = set()
        for item in self.environment:
            if not isinstance(item, Sequence) or len(item) != 2:
                raise SimulatorStackError("environment entries must be [name, value] pairs")
            key, value = item
            if not isinstance(key, str) or _SAFE_ENV_KEY.fullmatch(key) is None:
                raise SimulatorStackError("environment keys contain unsupported characters")
            if not isinstance(value, str) or key in keys:
                raise SimulatorStackError("environment contains invalid/duplicate entries")
            keys.add(key)
        bindings: list[tuple[str, int, int]] = []
        binding_ids: set[str] = set()
        for item in self.sensor_port_bindings:
            if (
                not isinstance(item, Sequence)
                or isinstance(item, (str, bytes, bytearray))
                or len(item) != 3
            ):
                raise SimulatorStackError(
                    "sensor_port_bindings entries must be [sensor_id, manifest_port, bound_port]"
                )
            sensor_id, manifest_port, bound_port = item
            if not isinstance(sensor_id, str) or not sensor_id:
                raise SimulatorStackError("sensor_port_bindings sensor_id must be non-empty")
            if sensor_id in binding_ids:
                raise SimulatorStackError("sensor_port_bindings contains duplicate sensor ids")
            binding_ids.add(sensor_id)
            try:
                source = _validate_port(manifest_port, "sensor manifest port")
                bound = _validate_port(bound_port, "sensor bound port")
            except (TypeError, ValueError) as exc:
                raise SimulatorStackError("sensor_port_bindings ports must be integers") from exc
            if source != DEFAULT_PX4_TELEMETRY_PORT:
                raise SimulatorStackError(
                    "sensor_port_bindings may only remap the canonical PX4 telemetry port"
                )
            if bound == PROTECTED_GROUND_STATION_PORT:
                raise SimulatorStackError(
                    f"sensor bound port cannot use protected UDP {PROTECTED_GROUND_STATION_PORT}"
                )
            bindings.append((sensor_id, source, bound))
        object.__setattr__(self, "sensor_port_bindings", tuple(bindings))
        root = self.repository_root.resolve()
        _assert_contained_file(self.world_path, root, "world_path")
        _assert_contained_file(self.map_path, root, "map_path")
        _assert_contained_file(self.dem_path, root, "dem_path")
        _assert_contained_file(self.model_path, root, "model_path")
        _assert_contained_file(self.assets.model_config_path, root, "model_config_path")
        if self.assets.repository_root.resolve() != root:
            raise SimulatorStackError("assets.repository_root must match repository_root")
        expected_model_root = (root / "simulation" / "gazebo" / "models").resolve()
        if self.assets.model_root.root.resolve() != expected_model_root:
            raise SimulatorStackError(
                "assets.model_root must match the repository Gazebo model directory"
            )
        if not self.assets.model_root.files:
            raise SimulatorStackError("assets.model_root must bind at least one file")
        if self.map_path.resolve() != self.assets.map_path.resolve():
            raise SimulatorStackError("map_path must match the resolved map asset")
        if self.dem_path.resolve() != self.assets.dem_path.resolve():
            raise SimulatorStackError("dem_path must match the resolved DEM asset")

    @property
    def environment_mapping(self) -> dict[str, str]:
        return dict(self.environment)

    @property
    def asset_sha256(self) -> dict[str, str]:
        return self.assets.asset_sha256

    @property
    def assets_sha256(self) -> str:
        return self.assets.aggregate_sha256

    @property
    def evidence_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    @property
    def evidence(self) -> dict[str, object]:
        return self.to_mapping()

    def to_mapping(self) -> dict[str, object]:
        root = self.repository_root.resolve()
        # Keep the canonical nested structure for machine consumers while
        # exposing stable flat aliases for launchers and human-facing CLI
        # tooling.  The aliases intentionally point at the exact same values;
        # they are not a second source of truth.
        asset_sha256 = self.assets.asset_sha256
        commands = {
            "gazebo": list(self.gazebo_command),
            "px4": list(self.px4_command),
            "gazebo_flavor": self.gazebo_flavor,
        }
        result: dict[str, object] = {
            "schema_version": self.schema_version,
            "event": "simulator_stack_plan",
            "scenario_id": self.scenario_id,
            "scenario_version": self.scenario_version,
            "scenario_manifest_hash": self.scenario_manifest_hash,
            "seed": self.seed,
            "vehicle_model": self.vehicle_model,
            "airframe_profile_id": self.airframe_profile_id,
            "world_path": _display_path(self.world_path, root),
            "map_path": _display_path(self.map_path, root),
            "dem_path": _display_path(self.dem_path, root),
            "assets_sha256": self.assets.aggregate_sha256,
            "px4_command": list(self.px4_command),
            "gazebo_command": list(self.gazebo_command),
            "scenario": {
                "id": self.scenario_id,
                "version": self.scenario_version,
                "manifest_sha256": self.scenario_manifest_hash,
                "seed": self.seed,
            },
            "vehicle": {
                "class": self.vehicle_class,
                "model": self.vehicle_model,
                "airframe_profile_id": self.airframe_profile_id,
                "model_name": self.model_name,
            },
            "world": {
                "uri": self.world_uri,
                "name": self.world_name,
                "path": _display_path(self.world_path, root),
                "absolute_path": str(self.world_path),
            },
            "map": {
                "uri": self.map_uri,
                "path": _display_path(self.map_path, root),
                "absolute_path": str(self.map_path),
                "sha256": self.assets.map.sha256,
            },
            "dem": {
                "uri": self.dem_uri,
                "path": _display_path(self.dem_path, root),
                "absolute_path": str(self.dem_path),
                "sha256": self.assets.dem.sha256,
            },
            "repository": {
                "root": str(root),
                "owned": True,
                "network_assets": False,
                "truth_loaded": False,
            },
            "assets": self.assets.to_mapping(),
            "asset_sha256": asset_sha256,
            "ports": self.ports.to_mapping(),
            "commands": commands,
            "tools": {
                name: {
                    "command": self.gazebo_executable if name == "gazebo" else self.px4_executable,
                    "available": available,
                }
                for name, available in self.tool_capabilities
            },
            "environment": self.environment_mapping,
            "tool_capabilities": dict(self.tool_capabilities),
            "runtime": {
                "headless": self.headless,
                "realtime": self.realtime,
                "loopback_only": True,
                "protected_udp_14550_used": False,
                "flight_control_output_target": self.ports.px4_control_endpoint,
                "dry_run": not all(value for _, value in self.tool_capabilities),
            },
            "validation": {
                "gate_passed": True,
                "paths_contained": True,
                "assets_hashed": True,
                "sdf_validated": True,
                "map_validated": True,
                "dem_validated": True,
                "loopback_only": True,
                "ports_distinct": True,
                "protected_udp_14550_untouched": True,
            },
            "truth_free": True,
        }
        if self.sensor_port_bindings:
            result["sensor_port_bindings"] = [
                {
                    "sensor_id": sensor_id,
                    "manifest_port": manifest_port,
                    "bound_port": bound_port,
                    "reason": "canonical_px4_telemetry_port_override",
                }
                for sensor_id, manifest_port, bound_port in self.sensor_port_bindings
            ]
        return result

    def to_mapping_with_hash(self) -> dict[str, object]:
        result = self.to_mapping()
        result["sha256"] = self.evidence_hash
        return result

    def as_dict(self) -> dict[str, object]:
        return self.to_mapping_with_hash()

    def write_json(self, path: str | Path, *, overwrite: bool = True) -> Path:
        target = Path(path).expanduser().resolve()
        if target.exists() and not overwrite:
            raise SimulatorStackError(f"evidence path already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_mapping_with_hash(), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        return target


StackPlan = SimulatorStackPlan
StackPorts = SimulatorStackPorts
StackAsset = SimulatorStackAsset


def parse_builtin_world_uri(
    world_uri: str,
    *,
    repository_root: str | Path | None = None,
    scenario_id: str | None = None,
) -> BuiltinWorldReference:
    """Resolve a literal ``builtin://gazebo/<scenario>`` URI to local SDF."""

    if not isinstance(world_uri, str):
        raise SimulatorStackError("world_asset must be a string")
    value = world_uri.strip()
    match = _BUILTIN_WORLD_RE.fullmatch(value)
    if match is None or "?" in value or "#" in value:
        raise SimulatorStackError("world_asset must be builtin://gazebo/<name>")
    name = match.group("name")
    if name.endswith(".sdf"):
        name = name[:-4]
    if not name or scenario_id is not None and name != scenario_id:
        raise SimulatorStackError(
            f"builtin world '{name}' does not match scenario_id '{scenario_id}'"
        )
    root = _resolve_repository_root(repository_root)
    path = root / "simulation" / "gazebo" / "worlds" / f"{name}.sdf"
    if not path.is_file():
        path = root / "simulation" / "gazebo" / "worlds" / "variants" / f"{name}.sdf"
    # New airframe fixtures are self-contained under the scenario boundary so
    # adding a model does not require changing the shared world asset tree.
    if not path.is_file():
        path = root / "configs" / "scenarios" / "airframes" / f"{name}.sdf"
    _assert_contained_file(path, root, "world asset")
    _validate_world_sdf(path, expected_name="default")
    return BuiltinWorldReference(value, name, path)


resolve_builtin_world = parse_builtin_world_uri
parse_world_uri = parse_builtin_world_uri


def parse_builtin_asset_uri(
    asset_uri: str,
    *,
    kind: str,
    repository_root: str | Path | None = None,
    scenario_id: str | None = None,
) -> BuiltinAssetReference:
    """Resolve one exact ``builtin://maps|terrain`` URI inside the repository."""

    if kind not in {"maps", "terrain"}:
        raise SimulatorStackError("builtin asset kind must be maps or terrain")
    if not isinstance(asset_uri, str):
        raise SimulatorStackError(f"{kind} asset URI must be a string")
    value = asset_uri.strip()
    match = _BUILTIN_ASSET_RE.fullmatch(value)
    if match is None or match.group("kind") != kind or "?" in value or "#" in value:
        raise SimulatorStackError(f"asset must use builtin://{kind}/<name>")
    name = match.group("name")
    suffix = ".geojson" if kind == "maps" else ".dem.json"
    if not name.endswith(suffix):
        raise SimulatorStackError(f"{kind} asset must end with {suffix}")
    asset_scenario_id = name[: -len(suffix)]
    if not asset_scenario_id or scenario_id is not None and asset_scenario_id != scenario_id:
        raise SimulatorStackError(
            f"builtin {kind} asset '{asset_scenario_id}' does not match scenario_id '{scenario_id}'"
        )
    root = _resolve_repository_root(repository_root)
    path = root / "simulation" / kind / name
    if not path.is_file():
        path = root / "configs" / "scenarios" / "airframes" / name
    _assert_contained_file(path, root, f"{kind} asset")
    return BuiltinAssetReference(value, kind, name, path)


def resolve_simulator_assets(
    runtime_input: ScenarioRuntimeInput,
    *,
    repository_root: str | Path | None = None,
) -> SimulatorStackAssets:
    """Resolve, validate and hash every world-facing runtime asset."""

    if not isinstance(runtime_input, ScenarioRuntimeInput):
        raise TypeError("runtime_input must be ScenarioRuntimeInput")
    root = _resolve_repository_root(repository_root)
    world = parse_builtin_world_uri(
        runtime_input.world.world_asset,
        repository_root=root,
        scenario_id=runtime_input.scenario_id,
    )
    coordinates = read_world_coordinates(world.path)
    if coordinates is None:
        raise SimulatorStackError("world asset does not provide a valid WGS-84 origin")
    map_asset = parse_builtin_asset_uri(
        runtime_input.world.map_asset,
        kind="maps",
        repository_root=root,
        scenario_id=runtime_input.scenario_id,
    )
    dem_asset = parse_builtin_asset_uri(
        runtime_input.world.dem_asset,
        kind="terrain",
        repository_root=root,
        scenario_id=runtime_input.scenario_id,
    )
    _validate_map_geojson(
        map_asset.path,
        expected_scenario_id=runtime_input.scenario_id,
        world_coordinates=coordinates,
    )
    _validate_dem_json(
        dem_asset.path,
        expected_scenario_id=runtime_input.scenario_id,
        world_coordinates=coordinates,
    )
    vehicle_class, model_name = _vehicle_spec(
        runtime_input.vehicle.model, runtime_input.vehicle.airframe_profile_id
    )
    model_dir = root / "simulation" / "gazebo" / "models" / model_name
    model_path = model_dir / "model.sdf"
    config_path = model_dir / "model.config"
    _assert_contained_file(model_path, root, "model asset")
    _assert_contained_file(config_path, root, "model.config asset")
    _validate_model_sdf(model_path, expected_name=model_name, vehicle_class=vehicle_class)
    _validate_model_config(config_path, expected_name=model_name)
    return _assets_for_paths(
        root,
        world.path,
        map_asset.path,
        dem_asset.path,
        model_path,
        config_path,
    )


resolve_stack_assets = resolve_simulator_assets


def build_stack_plan(
    scenario: str | Path | ScenarioRuntimeInput,
    *,
    repository_root: str | Path | None = None,
    ports: SimulatorStackPorts | Mapping[str, object] | None = None,
    gazebo_executable: str | Path = "gz",
    px4_executable: str | Path = "px4",
    gazebo_flavor: str | None = None,
    headless: bool = True,
    realtime: bool = True,
    px4_workdir: str | Path | None = None,
    tool_capabilities: Mapping[str, object] | None = None,
) -> SimulatorStackPlan:
    """Build a deterministic stack plan without starting external programs."""

    root = _resolve_repository_root(repository_root)
    runtime, _manifest = _load_runtime_input(scenario, root)
    selected_ports = _coerce_ports(ports)
    _validate_runtime_network(runtime, selected_ports)
    sensor_port_bindings = _sensor_port_bindings(runtime, selected_ports)
    assets = resolve_simulator_assets(runtime, repository_root=root)
    vehicle_class, model_name = _vehicle_spec(
        runtime.vehicle.model, runtime.vehicle.airframe_profile_id
    )
    gazebo_exe, flavor = _normalize_executable(
        gazebo_executable,
        name="gazebo_executable",
        allowed={"gz", "gazebo", "ign"},
        explicit_flavor=gazebo_flavor,
    )
    px4_exe, _ = _normalize_executable(
        px4_executable,
        name="px4_executable",
        allowed={"px4", "px4-sitl"},
        explicit_flavor=None,
    )
    environment = _build_environment(
        runtime,
        assets,
        selected_ports,
        model_name=model_name,
        headless=bool(headless),
        realtime=bool(realtime),
        px4_workdir=px4_workdir,
    )
    plan = SimulatorStackPlan(
        scenario_id=runtime.scenario_id,
        scenario_version=runtime.scenario_version,
        scenario_manifest_hash=runtime.scenario_manifest_hash,
        seed=runtime.seed,
        vehicle_class=vehicle_class,
        vehicle_model=runtime.vehicle.model,
        airframe_profile_id=runtime.vehicle.airframe_profile_id,
        gazebo_flavor=flavor,
        gazebo_executable=gazebo_exe,
        px4_executable=px4_exe,
        world_uri=runtime.world.world_asset,
        world_name="default",
        world_path=assets.world.path,
        map_uri=runtime.world.map_asset,
        map_path=assets.map.path,
        dem_uri=runtime.world.dem_asset,
        dem_path=assets.dem.path,
        model_name=model_name,
        model_path=assets.model.path,
        repository_root=root,
        assets=assets,
        ports=selected_ports,
        gazebo_command=_gazebo_command(
            flavor,
            gazebo_exe,
            assets.world.path,
            headless=bool(headless),
            realtime=bool(realtime),
        ),
        px4_command=(px4_exe, "-i", "0", "-s", "etc/init.d-posix/rcS"),
        environment=tuple(sorted(environment.items())),
        headless=bool(headless),
        realtime=bool(realtime),
        sensor_port_bindings=sensor_port_bindings,
    )
    return _with_tool_capabilities(plan, tool_capabilities)


plan_simulator_stack = build_stack_plan
create_stack_plan = build_stack_plan


def _with_tool_capabilities(
    plan: SimulatorStackPlan,
    supplied: Mapping[str, object] | None,
) -> SimulatorStackPlan:
    """Attach deterministic PX4/Gazebo availability without launching them."""

    if supplied is not None and not isinstance(supplied, Mapping):
        raise TypeError("tool_capabilities must be a mapping")
    values: dict[str, bool] = {}
    for name, executable in (
        ("gazebo", plan.gazebo_executable),
        ("px4", plan.px4_executable),
    ):
        raw: object = None if supplied is None else supplied.get(name)
        if raw is None and supplied is not None:
            # Accept the concrete flavour/command key as a convenience while
            # keeping the serialized schema canonical.
            aliases = (
                (
                    plan.gazebo_flavor,
                    Path(executable).name.lower().removesuffix(".exe"),
                )
                if name == "gazebo"
                else (
                    Path(executable).name.lower().removesuffix(".exe"),
                    "px4-sitl",
                )
            )
            for alias in aliases:
                if alias in supplied:
                    raw = supplied[alias]
                    break
        values[name] = _tool_available(executable, raw)
    unknown = set(supplied or ()) - {
        "gazebo",
        "px4",
        "px4-sitl",
        plan.gazebo_flavor,
        Path(plan.gazebo_executable).name.lower().removesuffix(".exe"),
    }
    if unknown:
        raise SimulatorStackError(
            f"tool_capabilities has unknown field(s): {', '.join(sorted(map(str, unknown)))}"
        )
    return replace(plan, tool_capabilities=tuple(sorted(values.items())))


def _tool_available(executable: str, supplied: object) -> bool:
    if supplied is not None:
        if isinstance(supplied, bool):
            return supplied
        if isinstance(supplied, Mapping):
            for key in ("available", "installed", "present", "found"):
                if key in supplied:
                    value = supplied[key]
                    if not isinstance(value, bool):
                        raise SimulatorStackError(f"tool capability {key} must be bool")
                    return value
            raise SimulatorStackError(
                "tool capability mapping requires available/installed boolean"
            )
        if isinstance(supplied, (str, Path)):
            candidate = Path(supplied).expanduser()
            return (
                candidate.is_file()
                if candidate.is_absolute()
                else shutil.which(str(supplied)) is not None
            )
        raise SimulatorStackError("tool capability must be bool, path, or mapping")
    candidate = Path(executable)
    if candidate.is_absolute():
        return candidate.is_file()
    return shutil.which(executable) is not None


def stack_plan_from_scenario(
    path: str | Path | ScenarioRuntimeInput,
    repo_root: str | Path | None = None,
    tool_capabilities: Mapping[str, object] | None = None,
) -> SimulatorStackPlan:
    """Resolve a scenario manifest into a serializable, dry-run-safe plan.

    Unlike a process launcher this function never probes sockets or starts a
    process.  Missing ``px4``/Gazebo binaries are recorded in ``tools`` and
    set ``runtime.dry_run`` rather than invalidating repository asset checks.
    """

    return build_stack_plan(
        path,
        repository_root=repo_root,
        tool_capabilities=tool_capabilities,
    )


def _load_runtime_input(
    source: str | Path | ScenarioRuntimeInput,
    repository_root: Path,
) -> tuple[ScenarioRuntimeInput, Path | None]:
    if isinstance(source, ScenarioRuntimeInput):
        return source, None
    if not isinstance(source, (str, Path)):
        raise TypeError("scenario must be a path or ScenarioRuntimeInput")
    path = Path(source).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()
    if path.name.endswith(".runtime.json"):
        sibling = path.with_name(path.name.removesuffix(".runtime.json") + ".scenario.json")
        if sibling.is_file():
            path = sibling
    _assert_contained_file(path, repository_root, "scenario manifest")
    try:
        runtime = load_runtime_scenario(path, mode=RuntimeMode.SIM)
    except Exception as exc:
        raise SimulatorStackError(f"invalid SIM scenario manifest: {exc}") from exc
    return runtime, path


def _coerce_ports(
    value: SimulatorStackPorts | Mapping[str, object] | None,
) -> SimulatorStackPorts:
    if value is None:
        return SimulatorStackPorts()
    return (
        value if isinstance(value, SimulatorStackPorts) else SimulatorStackPorts.from_mapping(value)
    )


def _resolve_repository_root(value: str | Path | None) -> Path:
    root = DEFAULT_REPOSITORY_ROOT if value is None else Path(value).expanduser()
    if not root.is_absolute():
        root = Path.cwd() / root
    root = root.resolve()
    if not root.is_dir() or not (root / "simulation" / "gazebo").is_dir():
        raise SimulatorStackError(f"repository root does not contain simulation/gazebo: {root}")
    return root


def _assert_contained_file(path: str | Path, root: str | Path, label: str) -> Path:
    root_path = Path(root).expanduser().resolve()
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root_path / candidate
    if candidate.is_symlink():
        raise SimulatorStackError(f"{label} must not be a symlink: {candidate}")
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root_path)
    except ValueError as exc:
        raise SimulatorStackError(f"{label} escapes repository root: {candidate}") from exc
    if not resolved.is_file():
        raise SimulatorStackError(f"{label} is not a file: {resolved}")
    return resolved


def _display_path(path: Path, root: Path | None) -> str:
    if root is not None:
        try:
            return path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            pass
    return str(path.resolve())


def _validate_loopback_host(host: object) -> str:
    if not isinstance(host, str) or host not in LOOPBACK_HOSTS:
        raise SimulatorStackError("SIM host must be literal loopback 127.0.0.1 or ::1")
    return host


def _endpoint_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def _validate_port(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1024 <= value <= 65535:
        raise SimulatorStackError(f"{name} must be an integer in 1024..65535")
    return value


def _validate_sha(value: object, name: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
        raise SimulatorStackError(f"{name} must be a SHA-256 hexadecimal string")
    return value.lower()


def _vehicle_spec(model: str, profile: str) -> tuple[str, str]:
    if not isinstance(model, str) or not isinstance(profile, str):
        raise SimulatorStackError("vehicle model/profile must be strings")
    model_lower = model.lower()
    profile_lower = profile.lower()
    identity = f"{model_lower} {profile_lower}"
    # Keep the explicit PX4 airframe names ahead of the broad class checks:
    # advanced_plane contains ``plane`` and the tailsitter/tiltrotor profiles
    # contain ``vtol`` in their family name but use different PX4 models.
    if "quadtailsitter" in identity or "quad_tailsitter" in identity:
        return "vtol", "quadtailsitter_mono_cam"
    if "tiltrotor" in identity or "tilt_rotor" in identity:
        return "vtol", "tiltrotor_mono_cam"
    if "advanced_plane" in identity or "advanced-plane" in identity:
        return "fixed-wing", "advanced_plane_mono_cam"
    # Check rotorcraft before the generic aircraft tokens.  The explicit
    # model/profile names keep a plain multirotor from being silently treated
    # as a VTOL just because both are rotor-bearing aircraft.
    if any(
        token in model_lower or token in profile_lower
        for token in (
            "multirotor",
            "multi-rotor",
            "multicopter",
            "quadcopter",
            "quad-copter",
            "x500",
        )
    ):
        return "multirotor", "x500_mono_cam"
    if "vtol" in model_lower or "vtol" in profile_lower:
        return "vtol", "standard_vtol_mono_cam"
    if (
        any(token in model_lower for token in ("fixed", "cessna", "plane", "airplane"))
        or "fixed_wing" in profile_lower
    ):
        return "fixed-wing", "rc_cessna_mono_cam"
    raise SimulatorStackError(f"unsupported SIM vehicle model/profile: {model!r}/{profile!r}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_repository_model_asset_bundle(
    model_root: str | Path,
    *,
    repository_root: str | Path | None = None,
) -> SimulatorModelAssetBundle:
    """Hash every file exposed through the read-only Gazebo model mount.

    A world may reference an auxiliary mesh or texture that is not part of the
    selected airframe directory. Binding the complete repository-owned mount
    prevents those bytes from changing underneath an otherwise identical SIM
    plan or sensor record.
    """

    repository = _resolve_repository_root(repository_root)
    candidate = Path(model_root).expanduser()
    if not candidate.is_absolute():
        candidate = repository / candidate
    if candidate.is_symlink():
        raise SimulatorStackError("Gazebo model root must not be a symlink")
    root = candidate.resolve()
    expected = (repository / "simulation" / "gazebo" / "models").resolve()
    if root != expected or not root.is_dir():
        raise SimulatorStackError(
            "Gazebo model root must be the repository simulation/gazebo/models directory"
        )
    paths = sorted(
        (path for path in root.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    if not paths:
        raise SimulatorStackError("repository Gazebo model root is empty")
    records: list[SimulatorStackAsset] = []
    for path in paths:
        if path.is_symlink():
            raise SimulatorStackError(f"Gazebo model asset must not be a symlink: {path}")
        _assert_contained_file(path, root, "Gazebo model asset")
        records.append(
            SimulatorStackAsset(
                "gazebo_model_asset",
                path.resolve(),
                _sha256_file(path),
                path.stat().st_size,
            )
        )
    portable_files = [
        {
            "role": item.role,
            "path": _display_path(item.path, root),
            "sha256": item.sha256,
            "size_bytes": item.size_bytes,
        }
        for item in records
    ]
    aggregate = canonical_sha256(
        {
            "schema_version": 1,
            "file_count": len(portable_files),
            "files": portable_files,
        }
    )
    return SimulatorModelAssetBundle(root, tuple(records), aggregate)


def _assets_for_paths(
    root: Path,
    world_path: Path,
    map_path: Path,
    dem_path: Path,
    model_path: Path,
    config_path: Path,
) -> SimulatorStackAssets:
    model_root = resolve_repository_model_asset_bundle(
        model_path.parent.parent,
        repository_root=root,
    )
    records = (
        SimulatorStackAsset(
            "world_sdf", world_path, _sha256_file(world_path), world_path.stat().st_size
        ),
        SimulatorStackAsset(
            "map_geojson", map_path, _sha256_file(map_path), map_path.stat().st_size
        ),
        SimulatorStackAsset(
            "terrain_dem", dem_path, _sha256_file(dem_path), dem_path.stat().st_size
        ),
        SimulatorStackAsset(
            "model_sdf", model_path, _sha256_file(model_path), model_path.stat().st_size
        ),
        SimulatorStackAsset(
            "model_config",
            config_path,
            _sha256_file(config_path),
            config_path.stat().st_size,
        ),
    )
    aggregate = canonical_sha256(
        {
            "schema_version": 1,
            "files": [
                {
                    "role": item.role,
                    "path": _display_path(item.path, root),
                    "sha256": item.sha256,
                }
                for item in records
            ],
            "model_root_assets_sha256": model_root.aggregate_sha256,
        }
    )
    return SimulatorStackAssets(
        root,
        records[0],
        records[1],
        records[2],
        records[3],
        records[4],
        model_root,
        aggregate,
    )


def _read_json_asset(path: Path, label: str) -> tuple[dict[str, object], str]:
    try:
        text = path.read_text(encoding="utf-8")
        raw = json.loads(text)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SimulatorStackError(f"{label} is not valid UTF-8 JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise SimulatorStackError(f"{label} must be a JSON object")
    if _NETWORK_URI_RE.search(text):
        raise SimulatorStackError(f"{label} contains a network URI: {path}")
    return raw, text


def _finite_asset_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SimulatorStackError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise SimulatorStackError(f"{label} must be a finite number")
    return result


def _geojson_coordinate_pairs(value: object, label: str) -> list[tuple[float, float]]:
    if not isinstance(value, list) or not value:
        raise SimulatorStackError(f"{label} must contain coordinates")
    if len(value) in {2, 3} and all(
        not isinstance(item, bool) and isinstance(item, (int, float)) for item in value
    ):
        longitude = _finite_asset_number(value[0], f"{label} longitude")
        latitude = _finite_asset_number(value[1], f"{label} latitude")
        if not -180.0 <= longitude <= 180.0 or not -90.0 < latitude < 90.0:
            raise SimulatorStackError(f"{label} is outside the supported WGS-84 domain")
        if len(value) == 3:
            _finite_asset_number(value[2], f"{label} altitude")
        return [(longitude, latitude)]
    pairs: list[tuple[float, float]] = []
    for index, item in enumerate(value):
        pairs.extend(_geojson_coordinate_pairs(item, f"{label}[{index}]"))
    return pairs


def _validate_map_geojson(
    path: Path,
    *,
    expected_scenario_id: str,
    world_coordinates: tuple[float, float, float],
) -> None:
    raw, _text = _read_json_asset(path, "map GeoJSON")
    if raw.get("type") != "FeatureCollection":
        raise SimulatorStackError("map GeoJSON must be a FeatureCollection")
    if raw.get("schema_version") != 1 or raw.get("scenario_id") != expected_scenario_id:
        raise SimulatorStackError("map GeoJSON scenario identity is invalid")
    if raw.get("coordinate_reference") != "WGS84":
        raise SimulatorStackError("map GeoJSON coordinate_reference must be WGS84")
    features = raw.get("features")
    if not isinstance(features, list) or not features:
        raise SimulatorStackError("map GeoJSON must contain at least one feature")
    pairs: list[tuple[float, float]] = []
    for index, feature in enumerate(features):
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            raise SimulatorStackError(f"map GeoJSON feature {index} is invalid")
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict) or not isinstance(geometry.get("type"), str):
            raise SimulatorStackError(f"map GeoJSON feature {index} geometry is invalid")
        pairs.extend(
            _geojson_coordinate_pairs(geometry.get("coordinates"), f"map GeoJSON feature {index}")
        )
    world_latitude, world_longitude, _world_elevation = world_coordinates
    longitudes = [pair[0] for pair in pairs]
    latitudes = [pair[1] for pair in pairs]
    if not (
        min(longitudes) <= world_longitude <= max(longitudes)
        and min(latitudes) <= world_latitude <= max(latitudes)
    ):
        raise SimulatorStackError("map GeoJSON does not cover the world WGS-84 origin")


def _validate_dem_json(
    path: Path,
    *,
    expected_scenario_id: str,
    world_coordinates: tuple[float, float, float],
) -> None:
    try:
        grid = load_local_dem_grid(path)
    except TerrainDemError as exc:
        raise SimulatorStackError(f"terrain DEM is invalid: {exc}") from exc
    if grid.scenario_id != expected_scenario_id:
        raise SimulatorStackError("terrain DEM scenario identity is invalid")
    world_latitude, world_longitude, world_elevation = world_coordinates
    if (
        abs(grid.origin_latitude_deg - world_latitude) > 1.0e-9
        or abs(grid.origin_longitude_deg - world_longitude) > 1.0e-9
        or abs(grid.origin_elevation_m - world_elevation) > 1.0e-3
    ):
        raise SimulatorStackError("terrain DEM origin does not match the world WGS-84 origin")


def _validate_world_sdf(path: Path, *, expected_name: str) -> None:
    text = path.read_text(encoding="utf-8")
    if _NETWORK_URI_RE.search(text):
        raise SimulatorStackError(f"world SDF contains a network URI: {path}")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise SimulatorStackError(f"world SDF is not valid XML: {path}") from exc
    if root.tag != "sdf" or root.attrib.get("version") is None:
        raise SimulatorStackError("world SDF must have an <sdf version=...> root")
    world = root.find("world")
    if world is None or world.attrib.get("name") != expected_name:
        raise SimulatorStackError(f"world SDF must define world name {expected_name!r}")
    if world.find("spherical_coordinates") is None:
        raise SimulatorStackError("world SDF must define WGS-84 spherical_coordinates")
    # Mainline worlds may include the repository-owned neutral 3-D target.
    # Vehicle composition remains the job's explicit model; allowing this one
    # local scene asset keeps target geometry reusable without accepting Fuel,
    # network, or arbitrary model URIs into a deterministic stack plan.
    allowed_repository_models = {"model://multidetect_search_target_v1"}
    for include in world.findall(".//include"):
        uri = (include.findtext("uri") or "").strip()
        if uri and not uri.startswith("builtin://") and uri not in allowed_repository_models:
            raise SimulatorStackError(f"world include is not repository-owned: {uri}")


def _validate_model_sdf(path: Path, *, expected_name: str, vehicle_class: str) -> None:
    text = path.read_text(encoding="utf-8")
    if _NETWORK_URI_RE.search(text):
        raise SimulatorStackError(f"model SDF contains a network URI: {path}")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise SimulatorStackError(f"model SDF is not valid XML: {path}") from exc
    if root.tag != "sdf":
        raise SimulatorStackError("model SDF must have an <sdf> root")
    model = root.find("model")
    if model is None or model.attrib.get("name") != expected_name:
        raise SimulatorStackError(f"model SDF must define model name {expected_name!r}")
    includes = {
        (item.findtext("uri") or "").strip()
        for item in model.findall("include")
        if item.findtext("uri")
    }
    base_by_model = {
        "x500_mono_cam": "model://x500",
        "standard_vtol_mono_cam": "model://standard_vtol",
        "rc_cessna_mono_cam": "model://rc_cessna",
        "advanced_plane_mono_cam": "model://advanced_plane",
        "quadtailsitter_mono_cam": "model://quadtailsitter",
        "tiltrotor_mono_cam": "model://tiltrotor",
    }
    try:
        base = base_by_model[expected_name]
    except KeyError as exc:
        raise SimulatorStackError(
            f"unsupported simulator model composition: {expected_name}"
        ) from exc
    if base not in includes or "model://mono_cam" not in includes:
        raise SimulatorStackError(f"{vehicle_class} model is missing required local compositions")
    for uri in includes:
        if not uri.startswith("model://") or _SAFE_IDENTIFIER_RE.fullmatch(uri[8:]) is None:
            raise SimulatorStackError(f"model dependency is not a safe local URI: {uri}")
        allowed = {base, "model://mono_cam"}
        if uri not in allowed:
            raise SimulatorStackError(f"model dependency is not an approved local model: {uri}")


def _validate_model_config(path: Path, *, expected_name: str) -> None:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise SimulatorStackError(f"model.config is not valid XML: {path}") from exc
    if root.tag != "model" or root.findtext("name") != expected_name:
        raise SimulatorStackError(f"model.config must name {expected_name!r}")
    sdf = root.find("sdf")
    if sdf is None or (sdf.text or "").strip() != "model.sdf":
        raise SimulatorStackError("model.config must point to model.sdf")


def _validate_runtime_network(runtime: ScenarioRuntimeInput, ports: SimulatorStackPorts) -> None:
    for value, name, kind in (
        (runtime.world.map_asset, "map_asset", "maps"),
        (runtime.world.dem_asset, "dem_asset", "terrain"),
    ):
        match = _BUILTIN_ASSET_RE.fullmatch(value)
        if match is None or match.group("kind") != kind:
            raise SimulatorStackError(f"{name} must be a builtin {kind} URI")
        if re.search(r"(?:^|[/_.-])truth(?:$|[/_.-])", value, re.IGNORECASE):
            raise SimulatorStackError(f"{name} cannot reference truth/evaluator assets")
    sensor_ids: set[str] = set()
    for sensor in runtime.sensors:
        if sensor.sensor_id in sensor_ids:
            raise SimulatorStackError(f"duplicate sensor_id: {sensor.sensor_id}")
        sensor_ids.add(sensor.sensor_id)
        _validate_sensor_uri(sensor.stream_uri, ports, sensor_kind=sensor.kind)


def _validate_sensor_uri(
    uri: str,
    ports: SimulatorStackPorts,
    *,
    sensor_kind: str | None = None,
) -> None:
    if not isinstance(uri, str) or not uri.strip():
        raise SimulatorStackError("sensor stream_uri must be non-empty")
    value = uri.strip()
    if re.search(r"(?:^|[/_.-])(?:truth|groundtruth)(?:$|[/_.-])", value, re.IGNORECASE):
        raise SimulatorStackError("sensor stream_uri cannot reference truth/evaluator endpoints")
    parsed = urlparse(value)
    if parsed.scheme == "gazebo":
        if not parsed.netloc or _SAFE_IDENTIFIER_RE.fullmatch(parsed.netloc) is None:
            raise SimulatorStackError(f"sensor gazebo URI is not local: {uri}")
        return
    if parsed.scheme != "udp" or parsed.username or parsed.password:
        raise SimulatorStackError(f"sensor stream_uri is not isolated UDP: {uri}")
    if parsed.hostname not in LOOPBACK_HOSTS:
        raise SimulatorStackError("SIM UDP sensor endpoints require literal loopback")
    try:
        port = int(parsed.port or 0)
    except ValueError as exc:
        raise SimulatorStackError(f"sensor UDP URI has invalid port: {uri}") from exc
    _validate_port(port, "sensor stream port")
    if port == PROTECTED_GROUND_STATION_PORT:
        raise SimulatorStackError("sensor stream cannot use protected UDP 14550")
    if sensor_kind == "mavlink_telemetry":
        # The checked-in runtime channel deliberately uses the canonical
        # 14541 endpoint.  Per-job launchers rewrite PX4's telemetry output to
        # their selected port, so accept that one explicit remap while keeping
        # arbitrary telemetry endpoints rejected.
        if port == ports.px4_telemetry_port:
            return
        if (
            port == DEFAULT_PX4_TELEMETRY_PORT
            and ports.px4_telemetry_port != DEFAULT_PX4_TELEMETRY_PORT
        ):
            return
        raise SimulatorStackError(
            "mavlink_telemetry sensor must use the selected PX4 telemetry port "
            f"{ports.px4_telemetry_port} (manifest may use canonical {DEFAULT_PX4_TELEMETRY_PORT})"
        )
    if port not in ports.all_ports:
        raise SimulatorStackError(f"sensor UDP port {port} is not owned by this stack")


def _sensor_port_bindings(
    runtime: ScenarioRuntimeInput,
    ports: SimulatorStackPorts,
) -> tuple[tuple[str, int, int], ...]:
    """Return explicit canonical-to-job telemetry port remaps.

    Runtime manifests remain hash-stable and portable by referring to
    ``127.0.0.1:14541``.  The selected stack port is the actual PX4 launch
    binding; retaining this mapping in the plan prevents a custom Docker job
    from silently disagreeing with the manifest's channel description.
    """

    if ports.px4_telemetry_port == DEFAULT_PX4_TELEMETRY_PORT:
        return ()
    bindings: list[tuple[str, int, int]] = []
    for sensor in runtime.sensors:
        if sensor.kind != "mavlink_telemetry":
            continue
        parsed = urlparse(sensor.stream_uri.strip())
        if parsed.scheme != "udp" or parsed.hostname not in LOOPBACK_HOSTS:
            continue
        try:
            source_port = int(parsed.port or 0)
        except ValueError:
            continue
        if source_port == DEFAULT_PX4_TELEMETRY_PORT:
            bindings.append(
                (sensor.sensor_id, DEFAULT_PX4_TELEMETRY_PORT, ports.px4_telemetry_port)
            )
    return tuple(bindings)


def _normalize_executable(
    value: str | Path,
    *,
    name: str,
    allowed: set[str],
    explicit_flavor: str | None,
) -> tuple[str, str]:
    raw = str(value).strip() if isinstance(value, (str, Path)) else ""
    if not raw:
        raise SimulatorStackError(f"{name} must not be empty")
    path = Path(raw).expanduser()
    if path.is_absolute():
        if not path.is_file():
            raise SimulatorStackError(f"{name} absolute path is not an existing file: {path}")
        executable = str(path.resolve())
        stem = path.name.lower().removesuffix(".exe")
    else:
        if "/" in raw or "\\" in raw or _SAFE_COMMAND_RE.fullmatch(raw) is None:
            raise SimulatorStackError(f"{name} must be a bare safe command name or absolute path")
        executable = raw
        stem = Path(raw).name.lower().removesuffix(".exe")
    if stem not in allowed:
        raise SimulatorStackError(f"{name} must be one of {sorted(allowed)}, got {stem!r}")
    if explicit_flavor is not None:
        flavor = explicit_flavor.lower()
        if flavor not in {"gz", "gazebo", "ign"} or stem != flavor:
            raise SimulatorStackError(f"{name} conflicts with gazebo_flavor {explicit_flavor!r}")
        return executable, flavor
    return executable, stem


def _gazebo_command(
    flavor: str,
    executable: str,
    world_path: Path,
    *,
    headless: bool,
    realtime: bool,
) -> tuple[str, ...]:
    if flavor == "gz":
        args = [executable, "sim"]
        if realtime:
            args.append("-r")
        if headless:
            args.append("--headless-rendering")
        args.extend(("-v", "3", str(world_path)))
        return tuple(args)
    if flavor == "gazebo":
        args = [executable, "--verbose"]
        if not realtime:
            args.append("--pause")
        args.append(str(world_path))
        return tuple(args)
    args = [executable, "gazebo"]
    if realtime:
        args.append("-r")
    args.append(str(world_path))
    return tuple(args)


def _build_environment(
    runtime: ScenarioRuntimeInput,
    assets: SimulatorStackAssets,
    ports: SimulatorStackPorts,
    *,
    model_name: str,
    headless: bool,
    realtime: bool,
    px4_workdir: str | Path | None,
) -> dict[str, str]:
    model_root = assets.model_path.parent.parent
    world_root = assets.world_path.parent
    # PX4's stock Gazebo airframes use separate autostart profiles.  Keep the
    # mapping explicit so a new model cannot inherit the fixed-wing profile by
    # falling through a boolean ``else`` branch.
    autostart = {
        "standard_vtol_mono_cam": "4004",
        "rc_cessna_mono_cam": "4003",
        "x500_mono_cam": "4001",
        "advanced_plane_mono_cam": "4008",
        "quadtailsitter_mono_cam": "4018",
        "tiltrotor_mono_cam": "4020",
    }.get(model_name)
    gimbal_profile = runtime.vehicle.airframe_profile_id.lower() in {
        "x500_gimbal",
        "px4_x500_gimbal",
    }
    if gimbal_profile:
        autostart = "4019"
    if autostart is None:
        raise SimulatorStackError(f"unsupported simulator model for PX4 autostart: {model_name}")
    environment = {
        "HEADLESS": "1" if headless else "0",
        "PX4_SYS_AUTOSTART": autostart,
        "PX4_SIM_MODEL": "gz_x500_gimbal" if gimbal_profile else f"gz_{model_name}",
        "PX4_GZ_WORLD": "default",
        "PX4_GZ_MODELS": str(model_root),
        "GZ_SIM_RESOURCE_PATH": f"{model_root}{os.pathsep}{world_root}",
        "PX4_SIM_HOST": ports.host,
        "PX4_MAVLINK_UDP_PORT": str(ports.px4_control_port),
        "PX4_MAVLINK_TELEMETRY_PORT": str(ports.px4_telemetry_port),
        "MD_SIM_SCENARIO_ID": runtime.scenario_id,
        "MD_SIM_SCENARIO_SHA256": runtime.scenario_manifest_hash,
        "MD_SIM_SEED": str(runtime.seed),
        "MD_SIM_REALTIME": "1" if realtime else "0",
        "MD_SIM_MAP_PATH": str(assets.map_path),
        "MD_SIM_MAP_SHA256": assets.map.sha256,
        "MD_SIM_DEM_PATH": str(assets.dem_path),
        "MD_SIM_DEM_SHA256": assets.dem.sha256,
    }
    spawn_pose = runtime.vehicle.spawn_pose
    if spawn_pose is not None:
        pose_values = (
            spawn_pose.x_east_m,
            spawn_pose.y_north_m,
            spawn_pose.z_up_m,
            spawn_pose.roll_rad,
            spawn_pose.pitch_rad,
            spawn_pose.yaw_rad,
        )
        environment["PX4_GZ_MODEL_POSE"] = ",".join(
            "0" if value == 0.0 else format(value, ".17g") for value in pose_values
        )
    coordinates = _world_coordinates(assets.world_path)
    if coordinates is not None:
        lat, lon, elevation = coordinates
        environment.update(
            {
                "PX4_HOME_LAT": f"{lat:.8f}",
                "PX4_HOME_LON": f"{lon:.8f}",
                "PX4_HOME_ALT": f"{elevation:.3f}",
            }
        )
    if px4_workdir is not None:
        workdir = Path(px4_workdir).expanduser()
        if not workdir.is_absolute():
            workdir = assets.repository_root / workdir
        workdir = workdir.resolve()
        try:
            workdir.relative_to(assets.repository_root.resolve())
        except ValueError as exc:
            raise SimulatorStackError("px4_workdir escapes repository root") from exc
        if not workdir.is_dir():
            raise SimulatorStackError(f"px4_workdir is not a directory: {workdir}")
        environment["PX4_WORKDIR"] = str(workdir)
    return environment


def read_world_coordinates(path: str | Path) -> tuple[float, float, float] | None:
    """Read the WGS-84 origin from a world SDF.

    The stack planner and deterministic scenario runner must use one geodetic
    frame.  Keeping this parser here avoids a second, subtly different XML
    interpretation in the runner.  ``None`` means the file is unavailable,
    malformed, or does not contain finite coordinates; callers retain their
    fail-closed/default behaviour in that case.
    """

    candidate = Path(path).expanduser()
    try:
        root = ET.parse(candidate.resolve()).getroot()
        # SDF files normally wrap ``<world>`` in an ``<sdf>`` root.  Accept a
        # world root as well so small standalone fixtures use the exact same
        # parser and do not silently fall back to Null Island.
        spherical = root.find("world/spherical_coordinates")
        if spherical is None and root.tag.rsplit("}", 1)[-1] == "world":
            spherical = root.find("spherical_coordinates")
        if spherical is None:
            return None
        values = tuple(
            float(spherical.findtext(name, "nan"))
            for name in ("latitude_deg", "longitude_deg", "elevation")
        )
    except (OSError, ET.ParseError, TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in values):
        return None
    # Exact poles are intentionally rejected across the SIM geodetic
    # pipeline: the local N/E tangent frame and longitude direction become
    # singular there.  Returning a pole origin would otherwise let the stack
    # planner claim a usable WGS-84 frame and fail only after model startup.
    if not -90.0 < values[0] < 90.0 or not -180.0 <= values[1] <= 180.0:
        return None
    return values  # type: ignore[return-value]


def _world_coordinates(path: Path) -> tuple[float, float, float] | None:
    """Backward-compatible private alias for the shared origin parser."""

    return read_world_coordinates(path)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point used by ``python -m multidetect.simulator stack-plan``."""

    parser = argparse.ArgumentParser(description="Plan an isolated PX4/Gazebo SIM stack")
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--repository-root", type=Path)
    parser.add_argument("--gazebo-executable", "--gazebo", default="gz")
    parser.add_argument("--px4-executable", "--px4", default="px4")
    parser.add_argument("--gazebo-flavor", choices=("gz", "gazebo", "ign"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--px4-control-port",
        "--px4-port",
        type=int,
        default=DEFAULT_PX4_CONTROL_PORT,
    )
    parser.add_argument("--px4-telemetry-port", type=int, default=DEFAULT_PX4_TELEMETRY_PORT)
    parser.add_argument("--qgc-output-port", type=int, default=DEFAULT_QGC_OUTPUT_PORT)
    parser.add_argument("--qgc-input-port", type=int, default=DEFAULT_QGC_INPUT_PORT)
    parser.add_argument("--metadata-port", type=int, default=DEFAULT_METADATA_PORT)
    parser.add_argument("--rtsp-port", type=int, default=DEFAULT_RTSP_PORT)
    parser.add_argument("--headless", dest="headless", action="store_true", default=True)
    parser.add_argument("--gui", dest="headless", action="store_false")
    parser.add_argument("--realtime", dest="realtime", action="store_true", default=True)
    parser.add_argument("--no-realtime", dest="realtime", action="store_false")
    parser.add_argument("--px4-workdir", type=Path)
    args = parser.parse_args(argv)
    try:
        plan = build_stack_plan(
            args.scenario,
            repository_root=args.repository_root,
            ports=SimulatorStackPorts(
                host=args.host,
                px4_control_port=args.px4_control_port,
                px4_telemetry_port=args.px4_telemetry_port,
                qgc_output_port=args.qgc_output_port,
                qgc_input_port=args.qgc_input_port,
                metadata_port=args.metadata_port,
                rtsp_port=args.rtsp_port,
            ),
            gazebo_executable=args.gazebo_executable,
            px4_executable=args.px4_executable,
            gazebo_flavor=args.gazebo_flavor,
            headless=args.headless,
            realtime=args.realtime,
            px4_workdir=args.px4_workdir,
        )
        if args.out is not None:
            plan.write_json(args.out)
        print(json.dumps(plan.to_mapping_with_hash(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (SimulatorStackError, OSError, TypeError, ValueError) as exc:
        print(f"simulator stack-plan: {exc}", file=os.sys.stderr)
        return 2


__all__ = [
    "BuiltinAssetReference",
    "BuiltinWorldReference",
    "DEFAULT_METADATA_PORT",
    "DEFAULT_PX4_CONTROL_PORT",
    "DEFAULT_PX4_TELEMETRY_PORT",
    "DEFAULT_QGC_INPUT_PORT",
    "DEFAULT_QGC_OUTPUT_PORT",
    "DEFAULT_REPOSITORY_ROOT",
    "DEFAULT_RTSP_PORT",
    "PROTECTED_GROUND_STATION_PORT",
    "SimulatorStackAsset",
    "SimulatorStackAssets",
    "SimulatorModelAssetBundle",
    "SimulatorStackError",
    "SimulatorStackIsolationError",
    "StackPlanError",
    "SimulatorStackPlan",
    "SimulatorStackPorts",
    "StackAsset",
    "StackPlan",
    "StackPorts",
    "airframe_profile_hash",
    "build_stack_plan",
    "create_stack_plan",
    "main",
    "parse_builtin_asset_uri",
    "parse_builtin_world_uri",
    "parse_world_uri",
    "plan_simulator_stack",
    "read_world_coordinates",
    "resolve_repository_model_asset_bundle",
    "resolve_builtin_world",
    "resolve_simulator_assets",
    "resolve_stack_assets",
    "stack_plan_from_scenario",
]
