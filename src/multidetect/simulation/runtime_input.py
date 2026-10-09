"""Scenario data that may cross into a runtime backend.

No target pose, class label or expected result lives in these types.  They are
safe to hand to the autonomy core without leaking the simulation answer.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import pi
from typing import Any

from ..autonomy.serialization import (
    AutonomyContractError,
    canonical_json,
    canonical_sha256,
    optional_text,
    require_array,
    require_int,
    require_real,
    require_schema_version,
    require_sha256,
    require_text,
    strict_object,
)
from .faults import FaultEvent


@dataclass(frozen=True, slots=True)
class WorldRuntimeInput:
    world_asset: str
    map_asset: str
    dem_asset: str
    wind_north_mps: float
    wind_east_mps: float
    gust_mps: float

    def __post_init__(self) -> None:
        for name in ("world_asset", "map_asset", "dem_asset"):
            object.__setattr__(self, name, require_text(getattr(self, name), name))
        object.__setattr__(
            self,
            "wind_north_mps",
            require_real(self.wind_north_mps, "wind_north_mps"),
        )
        object.__setattr__(
            self,
            "wind_east_mps",
            require_real(self.wind_east_mps, "wind_east_mps"),
        )
        object.__setattr__(self, "gust_mps", require_real(self.gust_mps, "gust_mps", minimum=0.0))

    @classmethod
    def from_mapping(cls, raw: object) -> WorldRuntimeInput:
        value = strict_object(
            raw,
            name="WorldRuntimeInput",
            required=frozenset(
                {
                    "world_asset",
                    "map_asset",
                    "dem_asset",
                    "wind_north_mps",
                    "wind_east_mps",
                    "gust_mps",
                }
            ),
        )
        return cls(
            world_asset=require_text(value["world_asset"], "world_asset"),
            map_asset=require_text(value["map_asset"], "map_asset"),
            dem_asset=require_text(value["dem_asset"], "dem_asset"),
            wind_north_mps=require_real(value["wind_north_mps"], "wind_north_mps"),
            wind_east_mps=require_real(value["wind_east_mps"], "wind_east_mps"),
            gust_mps=require_real(value["gust_mps"], "gust_mps", minimum=0.0),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "world_asset": self.world_asset,
            "map_asset": self.map_asset,
            "dem_asset": self.dem_asset,
            "wind_north_mps": self.wind_north_mps,
            "wind_east_mps": self.wind_east_mps,
            "gust_mps": self.gust_mps,
        }


@dataclass(frozen=True, slots=True)
class VehicleSpawnPose:
    """Hash-bound initial vehicle pose in the Gazebo ENU world frame.

    Position axes are named explicitly so callers cannot silently feed PX4
    NED values into Gazebo's X-east, Y-north, Z-up pose.  Euler angles are in
    radians because ``PX4_GZ_MODEL_POSE`` consumes radians.
    """

    x_east_m: float
    y_north_m: float
    z_up_m: float
    roll_rad: float
    pitch_rad: float
    yaw_rad: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "x_east_m",
            require_real(self.x_east_m, "x_east_m", minimum=-10_000.0, maximum=10_000.0),
        )
        object.__setattr__(
            self,
            "y_north_m",
            require_real(
                self.y_north_m,
                "y_north_m",
                minimum=-10_000.0,
                maximum=10_000.0,
            ),
        )
        object.__setattr__(
            self,
            "z_up_m",
            require_real(self.z_up_m, "z_up_m", minimum=0.0, maximum=1_000.0),
        )
        object.__setattr__(
            self,
            "roll_rad",
            require_real(self.roll_rad, "roll_rad", minimum=-pi, maximum=pi),
        )
        object.__setattr__(
            self,
            "pitch_rad",
            require_real(
                self.pitch_rad,
                "pitch_rad",
                minimum=-pi / 2.0,
                maximum=pi / 2.0,
            ),
        )
        object.__setattr__(
            self,
            "yaw_rad",
            require_real(self.yaw_rad, "yaw_rad", minimum=-pi, maximum=pi),
        )

    @classmethod
    def from_mapping(cls, raw: object) -> VehicleSpawnPose:
        fields = frozenset(
            {
                "x_east_m",
                "y_north_m",
                "z_up_m",
                "roll_rad",
                "pitch_rad",
                "yaw_rad",
            }
        )
        value = strict_object(raw, name="VehicleSpawnPose", required=fields)
        return cls(
            x_east_m=require_real(value["x_east_m"], "x_east_m"),
            y_north_m=require_real(value["y_north_m"], "y_north_m"),
            z_up_m=require_real(value["z_up_m"], "z_up_m"),
            roll_rad=require_real(value["roll_rad"], "roll_rad"),
            pitch_rad=require_real(value["pitch_rad"], "pitch_rad"),
            yaw_rad=require_real(value["yaw_rad"], "yaw_rad"),
        )

    def to_mapping(self) -> dict[str, float]:
        return {
            "x_east_m": self.x_east_m,
            "y_north_m": self.y_north_m,
            "z_up_m": self.z_up_m,
            "roll_rad": self.roll_rad,
            "pitch_rad": self.pitch_rad,
            "yaw_rad": self.yaw_rad,
        }


@dataclass(frozen=True, slots=True)
class VehicleEnergyProfile:
    """Runtime-visible energy assumptions; never evaluator-only target truth."""

    capacity_wh: float
    avionics_power_w: float
    propulsion_max_power_w: float
    provenance: str

    def __post_init__(self) -> None:
        for name in ("capacity_wh", "avionics_power_w", "propulsion_max_power_w"):
            object.__setattr__(self, name, require_real(getattr(self, name), name, minimum=0))
        if self.capacity_wh <= 0 or self.avionics_power_w + self.propulsion_max_power_w <= 0:
            raise AutonomyContractError("positive capacity and total power required")
        object.__setattr__(self, "provenance", require_text(self.provenance, "provenance"))

    @classmethod
    def from_mapping(cls, raw: object) -> VehicleEnergyProfile:
        value = strict_object(
            raw,
            name="VehicleEnergyProfile",
            required=frozenset(
                {
                    "capacity_wh",
                    "avionics_power_w",
                    "propulsion_max_power_w",
                    "provenance",
                }
            ),
        )
        return cls(**value)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "capacity_wh": self.capacity_wh,
            "avionics_power_w": self.avionics_power_w,
            "propulsion_max_power_w": self.propulsion_max_power_w,
            "provenance": self.provenance,
        }


@dataclass(frozen=True, slots=True)
class VehicleRuntimeInput:
    model: str
    airframe_profile_id: str
    spawn_id: str
    spawn_pose: VehicleSpawnPose | None = None
    energy_profile: VehicleEnergyProfile | None = None

    def __post_init__(self) -> None:
        for name in ("model", "airframe_profile_id", "spawn_id"):
            object.__setattr__(self, name, require_text(getattr(self, name), name))
        if self.spawn_pose is not None and not isinstance(self.spawn_pose, VehicleSpawnPose):
            raise AutonomyContractError("spawn_pose must be VehicleSpawnPose or None")
        if self.energy_profile is not None and not isinstance(
            self.energy_profile, VehicleEnergyProfile
        ):
            raise AutonomyContractError("energy_profile must be VehicleEnergyProfile or None")

    @classmethod
    def from_mapping(cls, raw: object) -> VehicleRuntimeInput:
        value = strict_object(
            raw,
            name="VehicleRuntimeInput",
            required=frozenset({"model", "airframe_profile_id", "spawn_id"}),
            optional=frozenset({"spawn_pose", "energy_profile"}),
        )
        return cls(
            model=require_text(value["model"], "model"),
            airframe_profile_id=require_text(value["airframe_profile_id"], "airframe_profile_id"),
            spawn_id=require_text(value["spawn_id"], "spawn_id"),
            energy_profile=(
                VehicleEnergyProfile.from_mapping(value["energy_profile"])
                if "energy_profile" in value
                else None
            ),
            spawn_pose=(
                VehicleSpawnPose.from_mapping(value["spawn_pose"])
                if "spawn_pose" in value
                else None
            ),
        )

    def to_mapping(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "model": self.model,
            "airframe_profile_id": self.airframe_profile_id,
            "spawn_id": self.spawn_id,
        }
        if self.spawn_pose is not None:
            result["spawn_pose"] = self.spawn_pose.to_mapping()
        if self.energy_profile is not None:
            result["energy_profile"] = self.energy_profile.to_mapping()
        return result


@dataclass(frozen=True, slots=True)
class SensorRuntimeInput:
    sensor_id: str
    kind: str
    stream_uri: str
    calibration_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("sensor_id", "kind", "stream_uri"):
            object.__setattr__(self, name, require_text(getattr(self, name), name))
        object.__setattr__(
            self,
            "calibration_id",
            optional_text(self.calibration_id, "calibration_id"),
        )

    @classmethod
    def from_mapping(cls, raw: object) -> SensorRuntimeInput:
        value = strict_object(
            raw,
            name="SensorRuntimeInput",
            required=frozenset({"sensor_id", "kind", "stream_uri"}),
            optional=frozenset({"calibration_id"}),
        )
        return cls(
            sensor_id=require_text(value["sensor_id"], "sensor_id"),
            kind=require_text(value["kind"], "kind"),
            stream_uri=require_text(value["stream_uri"], "stream_uri"),
            calibration_id=optional_text(value.get("calibration_id"), "calibration_id"),
        )

    def to_mapping(self) -> dict[str, Any]:
        result = {
            "sensor_id": self.sensor_id,
            "kind": self.kind,
            "stream_uri": self.stream_uri,
        }
        if self.calibration_id is not None:
            result["calibration_id"] = self.calibration_id
        return result


@dataclass(frozen=True, slots=True)
class ScenarioRuntimeInput:
    scenario_id: str
    scenario_version: int
    scenario_manifest_hash: str
    seed: int
    world: WorldRuntimeInput
    vehicle: VehicleRuntimeInput
    sensors: tuple[SensorRuntimeInput, ...]
    faults: tuple[FaultEvent, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", require_schema_version(self.schema_version))
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
        if not isinstance(self.world, WorldRuntimeInput):
            raise AutonomyContractError("world must be WorldRuntimeInput")
        if not isinstance(self.vehicle, VehicleRuntimeInput):
            raise AutonomyContractError("vehicle must be VehicleRuntimeInput")
        if not isinstance(self.sensors, Sequence) or isinstance(
            self.sensors, (str, bytes, bytearray)
        ):
            raise AutonomyContractError("sensors must be an array")
        sensors = tuple(self.sensors)
        if not sensors or any(not isinstance(item, SensorRuntimeInput) for item in sensors):
            raise AutonomyContractError("sensors must contain SensorRuntimeInput values")
        object.__setattr__(self, "sensors", sensors)
        if not isinstance(self.faults, Sequence) or isinstance(
            self.faults, (str, bytes, bytearray)
        ):
            raise AutonomyContractError("faults must be an array")
        faults = tuple(self.faults)
        if any(not isinstance(item, FaultEvent) for item in faults):
            raise AutonomyContractError("faults must contain FaultEvent values")
        object.__setattr__(self, "faults", faults)
        if tuple(sorted(item.at_s for item in self.faults)) != tuple(
            item.at_s for item in self.faults
        ):
            raise AutonomyContractError("scenario faults must be ordered by at_s")
        fault_ids = tuple(item.event_id for item in self.faults)
        if len(fault_ids) != len(set(fault_ids)):
            raise AutonomyContractError("scenario fault event_id values must be unique")

    @classmethod
    def from_mapping(
        cls,
        raw: object,
        *,
        scenario_manifest_hash: str,
    ) -> ScenarioRuntimeInput:
        value = strict_object(
            raw,
            name="ScenarioRuntimeInput",
            required=frozenset(
                {
                    "schema_version",
                    "scenario_id",
                    "scenario_version",
                    "seed",
                    "world",
                    "vehicle",
                    "sensors",
                    "faults",
                }
            ),
        )
        return cls(
            scenario_id=require_text(value["scenario_id"], "scenario_id"),
            scenario_version=require_int(value["scenario_version"], "scenario_version", minimum=1),
            scenario_manifest_hash=scenario_manifest_hash,
            seed=require_int(value["seed"], "seed", minimum=0),
            world=WorldRuntimeInput.from_mapping(value["world"]),
            vehicle=VehicleRuntimeInput.from_mapping(value["vehicle"]),
            sensors=tuple(
                SensorRuntimeInput.from_mapping(item)
                for item in require_array(value["sensors"], "sensors")
            ),
            faults=tuple(
                FaultEvent.from_mapping(item) for item in require_array(value["faults"], "faults")
            ),
            schema_version=require_schema_version(value["schema_version"]),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "scenario_version": self.scenario_version,
            "scenario_manifest_hash": self.scenario_manifest_hash,
            "schema_version": self.schema_version,
            "seed": self.seed,
            "world": self.world.to_mapping(),
            "vehicle": self.vehicle.to_mapping(),
            "sensors": [sensor.to_mapping() for sensor in self.sensors],
            "faults": [fault.to_mapping() for fault in self.faults],
        }

    def to_channel_mapping(self) -> dict[str, Any]:
        result = self.to_mapping()
        result.pop("scenario_manifest_hash")
        return result

    @property
    def canonical_json(self) -> str:
        return canonical_json(self.to_mapping())

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    @property
    def canonical_sha256(self) -> str:
        return self.canonical_hash

    @property
    def sha256(self) -> str:
        return self.canonical_hash


RuntimeScenarioInput = ScenarioRuntimeInput


__all__ = [
    "RuntimeScenarioInput",
    "ScenarioRuntimeInput",
    "SensorRuntimeInput",
    "VehicleRuntimeInput",
    "VehicleSpawnPose",
    "WorldRuntimeInput",
]
