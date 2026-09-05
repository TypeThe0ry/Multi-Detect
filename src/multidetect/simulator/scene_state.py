"""Hash-bound, truth-free 3-D simulator scene state streams.

The deterministic scenario runner already records telemetry and control
receipts, but those records are shaped as runner evidence rather than as a
small stream a renderer/replay tool can consume.  This module defines that
missing boundary.  A scene stream contains only the runtime channel: the
scenario identity, repository world origin, measured vehicle pose, sensor
observation, AI/manual/watchdog decision, optional compiled-Mission progress and
active sensor faults.

The stream is deliberately independent from QGC UI code.  The native QGC Fly
View, an external Gazebo bridge and a headless replay can all consume the same
JSON document.  Evaluator truth is rejected and never loaded here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from numbers import Real
from pathlib import Path

from ..autonomy.contracts import ExecutableMission
from ..autonomy.serialization import (
    AutonomyContractError,
    canonical_sha256,
    require_bool,
    require_int,
    require_real,
    require_schema_version,
    require_sha256,
    require_string_array,
    require_text,
    strict_json_loads,
    strict_object,
)
from ..mission_progress import MissionProgressSnapshot
from .flight_model import FlightMode

SCENE_STREAM_SCHEMA_VERSION = 1
SCENE_SNAPSHOT_SCHEMA_VERSION = 1
SCENE_STREAM_EVENT = "simulator_scene_stream_finished"
_MODES = frozenset(item.value for item in FlightMode)
_AUTHORITY_STATES = frozenset({"paused", "manual", "ai", "failsafe"})
_RELEASE_LABEL_PATTERN = re.compile(r"^0\.3\.0(?:-pre\.[1-9][0-9]*)?$")
_FORBIDDEN_RUNTIME_KEYS = frozenset(
    {
        "truth",
        "ground_truth",
        "evaluation_truth",
        "evaluator_truth",
        "ground_truth_target",
        "truth_targets",
        "expected_answer",
        "expectations",
        "truth_loaded",
    }
)


def _json_object(value: object, name: str) -> dict[str, object]:
    """Return a detached JSON-safe object and reject NaN/Infinity/odd keys."""

    if not isinstance(value, Mapping):
        raise AutonomyContractError(f"{name} must be an object")

    def reject_non_string_keys(nested: object, path: str) -> None:
        if isinstance(nested, Mapping):
            for key, child in nested.items():
                if not isinstance(key, str):
                    raise AutonomyContractError(f"{path} keys must be strings")
                reject_non_string_keys(child, f"{path}.{key}")
        elif isinstance(nested, Sequence) and not isinstance(nested, (str, bytes, bytearray)):
            for index, child in enumerate(nested):
                reject_non_string_keys(child, f"{path}[{index}]")

    reject_non_string_keys(value, name)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        detached = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AutonomyContractError(f"{name} contains a non-JSON value") from exc
    if not isinstance(detached, dict):  # pragma: no cover - guarded by Mapping above.
        raise AutonomyContractError(f"{name} must be an object")
    return detached


def _reject_truth_fields(value: object, name: str) -> None:
    """Reject evaluator/answer fields from a runtime-owned nested mapping."""

    if isinstance(value, Mapping):
        for key, nested in value.items():
            if isinstance(key, str) and key.lower() in _FORBIDDEN_RUNTIME_KEYS:
                raise AutonomyContractError(f"{name} contains forbidden truth field: {key}")
            _reject_truth_fields(nested, f"{name}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, nested in enumerate(value):
            _reject_truth_fields(nested, f"{name}[{index}]")


def _validate_release_label(value: object) -> str:
    release_label = require_text(value, "release_label")
    if _RELEASE_LABEL_PATTERN.fullmatch(release_label) is None:
        raise AutonomyContractError(
            "release_label must match 0.3.0 or 0.3.0-pre.<positive sequence>"
        )
    return release_label


def _optional_bool(value: object, name: str) -> bool | None:
    if value is None:
        return None
    return require_bool(value, name)


def _validate_world_origin(value: object) -> dict[str, object]:
    origin = _json_object(value, "world_origin")
    _reject_truth_fields(origin, "world_origin")
    latitude = require_real(origin.get("latitude_deg"), "world_origin.latitude_deg")
    longitude = require_real(origin.get("longitude_deg"), "world_origin.longitude_deg")
    if not -90.0 < latitude < 90.0:
        raise AutonomyContractError("world_origin.latitude_deg must be within (-90, 90)")
    if not -180.0 <= longitude <= 180.0:
        raise AutonomyContractError("world_origin.longitude_deg must be within [-180, 180]")
    if "elevation_m" in origin and origin["elevation_m"] is not None:
        require_real(origin["elevation_m"], "world_origin.elevation_m")
    if "resolved" in origin:
        require_bool(origin["resolved"], "world_origin.resolved")
    if "source" in origin:
        require_text(origin["source"], "world_origin.source")
    if "world_asset" in origin:
        require_text(origin["world_asset"], "world_origin.world_asset")
    return origin


def _validate_pose(value: object) -> dict[str, object]:
    pose = _json_object(value, "pose")
    _reject_truth_fields(pose, "pose")
    required = (
        "latitude_deg",
        "longitude_deg",
        "altitude_m",
        "north_m",
        "east_m",
        "heading_deg",
        "roll_deg",
        "pitch_deg",
        "yaw_deg",
        "ground_speed_mps",
        "airspeed_mps",
        "armed",
        "link_healthy",
        "flight_mode",
        "mission_started",
        "step_count",
    )
    strict_object(pose, name="pose", required=frozenset(required))
    latitude = require_real(pose["latitude_deg"], "pose.latitude_deg")
    longitude = require_real(pose["longitude_deg"], "pose.longitude_deg")
    if not -90.0 < latitude < 90.0:
        raise AutonomyContractError("pose.latitude_deg must be within (-90, 90)")
    if not -180.0 <= longitude <= 180.0:
        raise AutonomyContractError("pose.longitude_deg must be within [-180, 180]")
    for name in (
        "altitude_m",
        "north_m",
        "east_m",
        "ground_speed_mps",
        "airspeed_mps",
    ):
        number = require_real(pose[name], f"pose.{name}")
        if name in {"altitude_m", "ground_speed_mps", "airspeed_mps"} and number < 0.0:
            raise AutonomyContractError(f"pose.{name} cannot be negative")
    for name in ("heading_deg", "roll_deg", "pitch_deg", "yaw_deg"):
        require_real(pose[name], f"pose.{name}")
    _optional_bool(pose["armed"], "pose.armed")
    _optional_bool(pose["link_healthy"], "pose.link_healthy")
    require_text(pose["flight_mode"], "pose.flight_mode")
    require_bool(pose["mission_started"], "pose.mission_started")
    require_int(pose["step_count"], "pose.step_count", minimum=0)
    return pose


def _validate_observation(value: object) -> dict[str, object]:
    observation = _json_object(value, "observation")
    _reject_truth_fields(observation, "observation")
    if observation.get("truth_free") is not True:
        raise AutonomyContractError("observation.truth_free must be true")
    if "observed_at_s" not in observation:
        raise AutonomyContractError("observation requires observed_at_s")
    require_real(observation["observed_at_s"], "observation.observed_at_s", minimum=0.0)
    # ``None`` is the JSON representation of an unavailable GPS value.  Do
    # not instantiate SimulatorTelemetrySnapshot here: its constructor is
    # intentionally stricter for live callers, while a scene stream must
    # preserve fail-closed dropout observations exactly as observed.
    for name in ("latitude_deg", "longitude_deg"):
        if name in observation and observation[name] is not None:
            value_number = require_real(observation[name], f"observation.{name}")
            if name == "latitude_deg" and not -90.0 < value_number < 90.0:
                raise AutonomyContractError("observation.latitude_deg must be within (-90, 90)")
            if name == "longitude_deg" and not -180.0 <= value_number <= 180.0:
                raise AutonomyContractError("observation.longitude_deg must be within [-180, 180]")
    return observation


@dataclass(frozen=True, slots=True)
class SimulatorSceneSnapshot:
    """One hashable, truth-free state sample for a simulated 3-D scene."""

    sequence: int
    simulation_time_s: float
    scenario_id: str
    scenario_version: int
    scenario_manifest_hash: str
    mode: str
    pose: Mapping[str, object]
    telemetry: Mapping[str, object]
    observation: Mapping[str, object]
    decision: Mapping[str, object] | None = None
    control: Mapping[str, object] | None = None
    watchdog: str | None = None
    authority_state: str = "paused"
    active_fault_ids: tuple[str, ...] = ()
    schema_version: int = SCENE_SNAPSHOT_SCHEMA_VERSION
    truth_free: bool = True
    # Optional so streams produced before MissionProgress was introduced keep
    # their canonical representation and hashes.  When present, the value is
    # normalized to the same strict snapshot used by the closed loop.
    mission_progress: MissionProgressSnapshot | Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", require_schema_version(self.schema_version))
        object.__setattr__(self, "sequence", require_int(self.sequence, "sequence", minimum=0))
        object.__setattr__(
            self,
            "simulation_time_s",
            require_real(self.simulation_time_s, "simulation_time_s", minimum=0.0),
        )
        object.__setattr__(self, "scenario_id", require_text(self.scenario_id, "scenario_id"))
        object.__setattr__(
            self,
            "scenario_version",
            require_int(self.scenario_version, "scenario_version", minimum=1),
        )
        object.__setattr__(
            self,
            "scenario_manifest_hash",
            require_sha256(self.scenario_manifest_hash, "scenario_manifest_hash"),
        )
        normalized_mode = require_text(self.mode, "mode").lower().replace("_", "-")
        if normalized_mode not in _MODES:
            raise AutonomyContractError(f"mode must be one of {sorted(_MODES)}")
        object.__setattr__(self, "mode", normalized_mode)
        object.__setattr__(self, "pose", _validate_pose(self.pose))
        telemetry = _json_object(self.telemetry, "telemetry")
        _reject_truth_fields(telemetry, "telemetry")
        object.__setattr__(self, "telemetry", telemetry)
        object.__setattr__(self, "observation", _validate_observation(self.observation))
        if self.decision is not None:
            decision = _json_object(self.decision, "decision")
            _reject_truth_fields(decision, "decision")
            object.__setattr__(self, "decision", decision)
        if self.control is not None:
            control = _json_object(self.control, "control")
            _reject_truth_fields(control, "control")
            object.__setattr__(self, "control", control)
        if self.watchdog is not None:
            object.__setattr__(self, "watchdog", require_text(self.watchdog, "watchdog"))
        if self.mission_progress is not None:
            progress = (
                self.mission_progress
                if isinstance(self.mission_progress, MissionProgressSnapshot)
                else MissionProgressSnapshot.from_mapping(self.mission_progress)
            )
            object.__setattr__(self, "mission_progress", progress)
        authority = require_text(self.authority_state, "authority_state").lower()
        if authority not in _AUTHORITY_STATES:
            raise AutonomyContractError(
                f"authority_state must be one of {sorted(_AUTHORITY_STATES)}"
            )
        object.__setattr__(self, "authority_state", authority)
        object.__setattr__(
            self,
            "active_fault_ids",
            require_string_array(self.active_fault_ids, "active_fault_ids"),
        )
        if len(self.active_fault_ids) != len(set(self.active_fault_ids)):
            raise AutonomyContractError("active_fault_ids must be unique")
        object.__setattr__(self, "truth_free", require_bool(self.truth_free, "truth_free"))
        if not self.truth_free:
            raise AutonomyContractError("scene snapshots must be truth-free")

    @classmethod
    def from_mapping(cls, raw: object) -> SimulatorSceneSnapshot:
        value = strict_object(
            raw,
            name="SimulatorSceneSnapshot",
            required=frozenset(
                {
                    "schema_version",
                    "sequence",
                    "simulation_time_s",
                    "scenario_id",
                    "scenario_version",
                    "scenario_manifest_hash",
                    "mode",
                    "pose",
                    "telemetry",
                    "observation",
                    "decision",
                    "control",
                    "watchdog",
                    "authority_state",
                    "active_fault_ids",
                    "truth_free",
                }
            ),
            optional=frozenset({"mission_progress", "sha256"}),
        )
        snapshot = cls(
            schema_version=value["schema_version"],
            sequence=value["sequence"],
            simulation_time_s=value["simulation_time_s"],
            scenario_id=value["scenario_id"],
            scenario_version=value["scenario_version"],
            scenario_manifest_hash=value["scenario_manifest_hash"],
            mode=value["mode"],
            pose=value["pose"],
            telemetry=value["telemetry"],
            observation=value["observation"],
            decision=value["decision"],
            control=value["control"],
            watchdog=value["watchdog"],
            mission_progress=(
                MissionProgressSnapshot.from_mapping(value["mission_progress"])
                if "mission_progress" in value
                else None
            ),
            authority_state=value["authority_state"],
            active_fault_ids=require_string_array(value["active_fault_ids"], "active_fault_ids"),
            truth_free=value["truth_free"],
        )
        embedded = value.get("sha256")
        if embedded is not None and require_sha256(embedded, "sha256") != snapshot.canonical_hash:
            raise AutonomyContractError("scene snapshot sha256 does not match canonical content")
        return snapshot

    @classmethod
    def from_step_mapping(
        cls,
        step: Mapping[str, object],
        *,
        sequence: int,
        scenario_id: str,
        scenario_version: int,
        scenario_manifest_hash: str,
        mode: str,
        local_north_m: Real,
        local_east_m: Real,
        active_fault_ids: Sequence[str] = (),
    ) -> SimulatorSceneSnapshot:
        """Convert one runner step into a renderer/replay sample.

        ``telemetry_after`` is the measured vehicle state after the control
        tick; ``observation`` is the sensor-side input consumed by AI before
        that tick.  Local N/E offsets come from the deterministic model and
        are never exposed to the AI policy as evaluator truth.
        """

        if not isinstance(step, Mapping):
            raise AutonomyContractError("scene step must be an object")
        telemetry = step.get("telemetry_after")
        observation = step.get("observation")
        if not isinstance(telemetry, Mapping) or not isinstance(observation, Mapping):
            raise AutonomyContractError("scene step is missing telemetry_after/observation")
        time_value = step.get("simulation_time_s", telemetry.get("observed_at_s"))
        telemetry_copy = _json_object(telemetry, "telemetry_after")
        pose = {
            "latitude_deg": telemetry_copy.get("latitude_deg"),
            "longitude_deg": telemetry_copy.get("longitude_deg"),
            "altitude_m": telemetry_copy.get("altitude_agl_m"),
            "north_m": require_real(local_north_m, "local_north_m"),
            "east_m": require_real(local_east_m, "local_east_m"),
            "heading_deg": telemetry_copy.get("heading_deg"),
            "roll_deg": telemetry_copy.get("roll_deg", 0.0),
            "pitch_deg": telemetry_copy.get("pitch_deg", 0.0),
            "yaw_deg": telemetry_copy.get("yaw_deg", telemetry_copy.get("heading_deg")),
            "ground_speed_mps": telemetry_copy.get("ground_speed_mps"),
            "airspeed_mps": telemetry_copy.get(
                "airspeed_mps", telemetry_copy.get("ground_speed_mps")
            ),
            "armed": telemetry_copy.get("armed"),
            "link_healthy": telemetry_copy.get("link_healthy"),
            "flight_mode": telemetry_copy.get("flight_mode", "MANUAL"),
            "mission_started": telemetry_copy.get("mission_started", False),
            "step_count": telemetry_copy.get("step_count", 0),
        }
        decision = step.get("decision")
        control = step.get("control_receipt")
        authority = "paused"
        if isinstance(decision, Mapping) and isinstance(decision.get("authority_state"), str):
            authority = str(decision["authority_state"])
        elif isinstance(control, Mapping) and isinstance(control.get("state"), str):
            authority = str(control["state"])
        return cls(
            sequence=sequence,
            simulation_time_s=time_value,
            scenario_id=scenario_id,
            scenario_version=scenario_version,
            scenario_manifest_hash=scenario_manifest_hash,
            mode=mode,
            pose=pose,
            telemetry=telemetry_copy,
            observation=observation,
            decision=decision if isinstance(decision, Mapping) else None,
            control=control if isinstance(control, Mapping) else None,
            watchdog=step.get("watchdog") if isinstance(step.get("watchdog"), str) else None,
            mission_progress=(
                None
                if step.get("mission_progress") is None
                else MissionProgressSnapshot.from_mapping(step["mission_progress"])
            ),
            authority_state=authority,
            active_fault_ids=tuple(active_fault_ids),
        )

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    @property
    def sha256(self) -> str:
        return self.canonical_hash

    def to_mapping(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "simulation_time_s": self.simulation_time_s,
            "scenario_id": self.scenario_id,
            "scenario_version": self.scenario_version,
            "scenario_manifest_hash": self.scenario_manifest_hash,
            "mode": self.mode,
            "pose": dict(self.pose),
            "telemetry": dict(self.telemetry),
            "observation": dict(self.observation),
            "decision": None if self.decision is None else dict(self.decision),
            "control": None if self.control is None else dict(self.control),
            "watchdog": self.watchdog,
            "authority_state": self.authority_state,
            "active_fault_ids": list(self.active_fault_ids),
            "truth_free": True,
        }
        # Keep the field absent for legacy snapshots, preserving their
        # canonical hashes.  New mission-bound streams carry the canonical
        # progress mapping without an embedded second hash.
        if self.mission_progress is not None:
            result["mission_progress"] = self.mission_progress.to_mapping()
        return result

    def to_mapping_with_hash(self) -> dict[str, object]:
        result = self.to_mapping()
        result["sha256"] = self.canonical_hash
        return result


@dataclass(frozen=True, slots=True)
class SimulatorSceneStream:
    """A deterministic sequence of :class:`SimulatorSceneSnapshot` values."""

    scenario_id: str
    scenario_version: int
    scenario_manifest_hash: str
    seed: int
    mode: str
    release_label: str
    world_origin: Mapping[str, object]
    source_evidence_hash: str
    snapshots: tuple[SimulatorSceneSnapshot, ...]
    schema_version: int = SCENE_STREAM_SCHEMA_VERSION
    event: str = SCENE_STREAM_EVENT
    product: str = "Multi-Detect"
    deterministic: bool = True
    truth_free: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", require_schema_version(self.schema_version))
        if self.event != SCENE_STREAM_EVENT:
            raise AutonomyContractError(f"event must be {SCENE_STREAM_EVENT}")
        object.__setattr__(self, "product", require_text(self.product, "product"))
        if self.product != "Multi-Detect":
            raise AutonomyContractError("product must be 'Multi-Detect'")
        object.__setattr__(self, "scenario_id", require_text(self.scenario_id, "scenario_id"))
        object.__setattr__(
            self,
            "scenario_version",
            require_int(self.scenario_version, "scenario_version", minimum=1),
        )
        object.__setattr__(
            self,
            "scenario_manifest_hash",
            require_sha256(self.scenario_manifest_hash, "scenario_manifest_hash"),
        )
        object.__setattr__(self, "seed", require_int(self.seed, "seed", minimum=0))
        normalized_mode = require_text(self.mode, "mode").lower().replace("_", "-")
        if normalized_mode not in _MODES:
            raise AutonomyContractError(f"mode must be one of {sorted(_MODES)}")
        object.__setattr__(self, "mode", normalized_mode)
        object.__setattr__(self, "release_label", _validate_release_label(self.release_label))
        object.__setattr__(self, "world_origin", _validate_world_origin(self.world_origin))
        object.__setattr__(
            self,
            "source_evidence_hash",
            require_sha256(self.source_evidence_hash, "source_evidence_hash"),
        )
        if not isinstance(self.snapshots, Sequence) or isinstance(
            self.snapshots, (str, bytes, bytearray)
        ):
            raise AutonomyContractError("snapshots must be an array")
        snapshots = tuple(self.snapshots)
        if not snapshots:
            raise AutonomyContractError("snapshots must contain at least one sample")
        if any(not isinstance(item, SimulatorSceneSnapshot) for item in snapshots):
            raise AutonomyContractError("snapshots must contain SimulatorSceneSnapshot values")
        for index, snapshot in enumerate(snapshots):
            if snapshot.sequence != index:
                raise AutonomyContractError("scene snapshot sequence must be contiguous from zero")
            if (
                snapshot.scenario_id != self.scenario_id
                or snapshot.scenario_version != self.scenario_version
                or snapshot.scenario_manifest_hash != self.scenario_manifest_hash
                or snapshot.mode != self.mode
            ):
                raise AutonomyContractError("scene snapshot identity does not match stream")
            if index and snapshot.simulation_time_s < snapshots[index - 1].simulation_time_s:
                raise AutonomyContractError("scene snapshot simulation time moved backwards")
        progress_values = [snapshot.mission_progress for snapshot in snapshots]
        if any(value is not None for value in progress_values):
            if any(value is None for value in progress_values):
                raise AutonomyContractError(
                    "scene snapshots must all carry mission_progress when one is present"
                )
            identities = {
                (value.mission_id, value.mission_hash)
                for value in progress_values
                if value is not None
            }
            if len(identities) != 1:
                raise AutonomyContractError(
                    "scene snapshot mission_progress identity does not match stream"
                )
        object.__setattr__(self, "snapshots", snapshots)
        object.__setattr__(self, "deterministic", require_bool(self.deterministic, "deterministic"))
        if not self.deterministic:
            raise AutonomyContractError("scene stream must be deterministic")
        object.__setattr__(self, "truth_free", require_bool(self.truth_free, "truth_free"))
        if not self.truth_free:
            raise AutonomyContractError("scene stream must be truth-free")

    @classmethod
    def from_mapping(cls, raw: object) -> SimulatorSceneStream:
        value = strict_object(
            raw,
            name="SimulatorSceneStream",
            required=frozenset(
                {
                    "schema_version",
                    "event",
                    "product",
                    "scenario_id",
                    "scenario_version",
                    "scenario_manifest_hash",
                    "seed",
                    "mode",
                    "release_label",
                    "world_origin",
                    "source_evidence_hash",
                    "snapshot_count",
                    "snapshots",
                    "deterministic",
                    "truth_free",
                }
            ),
            optional=frozenset({"sha256"}),
        )
        snapshot_values = value["snapshots"]
        if not isinstance(snapshot_values, Sequence) or isinstance(
            snapshot_values, (str, bytes, bytearray)
        ):
            raise AutonomyContractError("snapshots must be an array")
        stream = cls(
            schema_version=value["schema_version"],
            event=value["event"],
            product=value["product"],
            scenario_id=value["scenario_id"],
            scenario_version=value["scenario_version"],
            scenario_manifest_hash=value["scenario_manifest_hash"],
            seed=value["seed"],
            mode=value["mode"],
            release_label=value["release_label"],
            world_origin=value["world_origin"],
            source_evidence_hash=value["source_evidence_hash"],
            snapshots=tuple(SimulatorSceneSnapshot.from_mapping(item) for item in snapshot_values),
            deterministic=value["deterministic"],
            truth_free=value["truth_free"],
        )
        count = require_int(value["snapshot_count"], "snapshot_count", minimum=1)
        if count != len(stream.snapshots):
            raise AutonomyContractError("snapshot_count does not match snapshots")
        embedded = value.get("sha256")
        if embedded is not None and require_sha256(embedded, "sha256") != stream.canonical_hash:
            raise AutonomyContractError("scene stream sha256 does not match canonical content")
        return stream

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    @property
    def sha256(self) -> str:
        return self.canonical_hash

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "event": self.event,
            "product": self.product,
            "scenario_id": self.scenario_id,
            "scenario_version": self.scenario_version,
            "scenario_manifest_hash": self.scenario_manifest_hash,
            "seed": self.seed,
            "mode": self.mode,
            "release_label": self.release_label,
            "world_origin": dict(self.world_origin),
            "source_evidence_hash": self.source_evidence_hash,
            "snapshot_count": len(self.snapshots),
            "snapshots": [item.to_mapping_with_hash() for item in self.snapshots],
            "deterministic": True,
            "truth_free": True,
        }

    def to_mapping_with_hash(self) -> dict[str, object]:
        result = self.to_mapping()
        result["sha256"] = self.canonical_hash
        return result

    def write_json(self, path: str | Path, *, overwrite: bool = True) -> Path:
        target = Path(path).expanduser().resolve()
        if target.exists() and not overwrite:
            raise AutonomyContractError(f"scene stream output already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_mapping_with_hash(), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        return target

    @classmethod
    def load_json(cls, path: str | Path) -> SimulatorSceneStream:
        """Load and validate a stream using duplicate-key/non-finite rejection."""

        target = Path(path).expanduser().resolve()
        try:
            raw = strict_json_loads(target.read_bytes(), name="simulator scene stream")
        except (OSError, TypeError, AutonomyContractError) as exc:
            if isinstance(exc, AutonomyContractError):
                raise
            raise AutonomyContractError(
                f"unable to load simulator scene stream {target}: {exc}"
            ) from exc
        return cls.from_mapping(raw)

    def replay(self) -> tuple[dict[str, object], ...]:
        """Return detached frame mappings in deterministic playback order."""

        return tuple(item.to_mapping_with_hash() for item in self.snapshots)


def scene_stream_from_result(result: object) -> SimulatorSceneStream:
    """Extract a recorded stream from a scenario result, fail-closed."""

    stream = getattr(result, "scene_stream", None)
    if isinstance(stream, SimulatorSceneStream):
        return stream
    # ``python -m multidetect.simulator.scene_state`` loads the package
    # ``__init__`` first.  That package imports the runner, which imports this
    # module, so runpy can briefly hold two module identities.  Re-parse the
    # detached mapping instead of relying only on ``isinstance``; this keeps
    # the CLI safe while retaining full structural/hash validation.
    if stream is not None and callable(getattr(stream, "to_mapping_with_hash", None)):
        try:
            return SimulatorSceneStream.from_mapping(stream.to_mapping_with_hash())
        except (AutonomyContractError, TypeError, ValueError) as exc:
            raise AutonomyContractError(f"invalid scenario scene stream: {exc}") from exc
    if stream is None:
        raise AutonomyContractError(
            "scenario result has no scene stream; enable record_scene_stream first"
        )
    raise AutonomyContractError("scenario result scene_stream has an invalid type")


def verify_file(
    path: str | Path,
    *,
    expected_release_label: str | None = None,
    schema_path: str | Path | None = None,
) -> dict[str, object]:
    """Verify one persisted scene stream and return a machine-readable report.

    The verifier deliberately reports both the byte-level file digest and the
    canonical stream digest.  The former proves which file was inspected; the
    latter proves the JSON content after parsing.  Schema validation is kept
    optional at the library boundary but enabled by default when the
    repository schema is present, so a source checkout gets the strongest
    available check without making ``jsonschema`` a runtime dependency.
    """

    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise AutonomyContractError(f"simulator scene stream does not exist: {source}")
    try:
        payload = source.read_bytes()
    except OSError as exc:
        raise AutonomyContractError(
            f"unable to read simulator scene stream {source}: {exc}"
        ) from exc
    raw = strict_json_loads(payload, name="simulator scene stream")
    if not isinstance(raw, Mapping):
        raise AutonomyContractError("simulator scene stream must contain a JSON object")
    stream = SimulatorSceneStream.from_mapping(raw)

    reasons: list[str] = []
    checks: dict[str, bool] = {
        "readable_json": True,
        "embedded_stream_hash": isinstance(raw.get("sha256"), str),
        "snapshot_hashes_present": all(
            isinstance(item, Mapping) and isinstance(item.get("sha256"), str)
            for item in raw.get("snapshots", ())
        ),
        "deterministic": stream.deterministic is True,
        "truth_free": stream.truth_free is True,
    }
    for name, passed in checks.items():
        if not passed:
            reasons.append(f"{name} check failed")

    if expected_release_label is not None:
        expected = _validate_release_label(expected_release_label)
        checks["release_label_matches"] = stream.release_label == expected
        if not checks["release_label_matches"]:
            reasons.append(
                f"release_label {stream.release_label!r} does not match expected {expected!r}"
            )

    resolved_schema = (
        Path(schema_path).expanduser().resolve()
        if schema_path is not None
        else Path(__file__).resolve().parents[3]
        / "configs"
        / "schemas"
        / "simulator-scene-stream.schema.json"
    )
    schema_checked = False
    schema_errors: list[str] = []
    if resolved_schema.is_file():
        schema_checked = True
        try:
            from jsonschema import Draft202012Validator

            schema = strict_json_loads(
                resolved_schema.read_bytes(), name="simulator scene stream schema"
            )
            Draft202012Validator.check_schema(schema)
            schema_errors = [
                error.message for error in Draft202012Validator(schema).iter_errors(raw)
            ]
        except Exception as exc:  # noqa: BLE001 - malformed schema must fail closed
            schema_errors = [f"schema validation failed: {type(exc).__name__}: {exc}"]
    elif schema_path is not None:
        schema_checked = True
        schema_errors = [f"schema does not exist: {resolved_schema}"]
    checks["json_schema"] = schema_checked and not schema_errors
    if schema_errors:
        reasons.extend(f"json_schema: {message}" for message in schema_errors)

    return {
        "event": "simulator_scene_stream_verified",
        "schema_version": SCENE_STREAM_SCHEMA_VERSION,
        "evidence_path": str(source),
        "file_sha256": hashlib.sha256(payload).hexdigest(),
        "file_bytes": len(payload),
        "scenario_id": stream.scenario_id,
        "scenario_version": stream.scenario_version,
        "scenario_manifest_hash": stream.scenario_manifest_hash,
        "mode": stream.mode,
        "release_label": stream.release_label,
        "snapshot_count": len(stream.snapshots),
        "source_evidence_hash": stream.source_evidence_hash,
        "canonical_sha256": stream.canonical_hash,
        "checks": checks,
        "schema_path": str(resolved_schema) if schema_checked else None,
        "schema_errors": schema_errors,
        "gate_passed": not reasons,
        "reasons": reasons,
    }


def verify_main(argv: list[str] | None = None) -> int:
    """CLI entry point for :func:`verify_file`."""

    parser = argparse.ArgumentParser(description="Verify one simulator scene stream artifact")
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--expected-release-label")
    parser.add_argument(
        "--schema",
        type=Path,
        default=None,
        help="optional JSON Schema path (the repository schema is used by default)",
    )
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        result = verify_file(
            args.evidence,
            expected_release_label=args.expected_release_label,
            schema_path=args.schema,
        )
    except (
        OSError,
        UnicodeError,
        AutonomyContractError,
        TypeError,
        ValueError,
        RuntimeError,
    ) as exc:
        result = {
            "event": "simulator_scene_stream_verified",
            "schema_version": SCENE_STREAM_SCHEMA_VERSION,
            "gate_passed": False,
            "checks": {"readable_json": False},
            "schema_errors": [],
            "reasons": [f"{type(exc).__name__}: {exc}"],
        }
    payload = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.out is not None:
        output = args.out.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if result.get("gate_passed") is True else 1


def main(argv: list[str] | None = None) -> int:
    """Run one deterministic scenario and emit its replayable scene stream."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument(
        "--mission",
        type=Path,
        help=(
            "optional ExecutableMission or PlannedMissionCompilation JSON to bind "
            "to each recorded scene snapshot"
        ),
    )
    parser.add_argument(
        "--start-mission",
        action="store_true",
        help="explicitly send MISSION_START after binding --mission",
    )
    parser.add_argument("--frames", type=int, default=120)
    parser.add_argument("--dt", type=float, default=0.1)
    parser.add_argument("--mode", choices=tuple(sorted(_MODES)))
    parser.add_argument("--no-ai", action="store_true")
    parser.add_argument("--no-arm", action="store_true")
    parser.add_argument("--fault", action="append", dest="fault_ids")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    # Local import keeps the public data types importable without pulling the
    # runner back into this module's validation path.
    from .scenario_runner import SimulatorScenarioRunner, SimulatorScenarioRunnerConfig

    try:
        mission = None if args.mission is None else _load_mission_document(args.mission)
        if args.start_mission and mission is None:
            raise ValueError("--start-mission requires --mission")
        runner = SimulatorScenarioRunner(
            args.scenario,
            config=SimulatorScenarioRunnerConfig(
                frames=args.frames,
                dt_s=args.dt,
                mode=args.mode,
                enable_ai=not args.no_ai,
                arm_on_start=not args.no_arm,
                active_fault_ids=None if args.fault_ids is None else tuple(args.fault_ids),
                record_scene_stream=True,
                history_limit=max(1, args.frames),
            ),
            mission=mission,
        )
        if args.start_mission:
            runner.start_mission()
        result = runner.run()
        stream = scene_stream_from_result(result)
    except (OSError, AutonomyContractError, TypeError, ValueError, RuntimeError) as exc:
        print(json.dumps({"event": "simulator_scene_stream_failed", "error": str(exc)}))
        return 2
    payload = (
        json.dumps(stream.to_mapping_with_hash(), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    )
    if args.out is not None:
        stream.write_json(args.out)
    print(payload, end="")
    return 0 if result.terminal_reason == "frame_budget_complete" else 1


def _load_mission_document(path: str | Path) -> ExecutableMission:
    """Load a strict mission or compilation artifact for the scene CLI."""

    from ..mission_planning import load_executable_mission_document

    return load_executable_mission_document(path)


__all__ = [
    "SCENE_SNAPSHOT_SCHEMA_VERSION",
    "SCENE_STREAM_EVENT",
    "SCENE_STREAM_SCHEMA_VERSION",
    "SimulatorSceneSnapshot",
    "SimulatorSceneStream",
    "main",
    "scene_stream_from_result",
    "verify_file",
    "verify_main",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
