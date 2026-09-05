"""Evaluation-only Ground Truth and expectation channels.

Neither type is imported by the runtime-only loader.  The simulator/evaluator
loads these files separately after the autonomy runtime input is constructed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ..autonomy.contracts import GeoPoint
from ..autonomy.serialization import (
    AutonomyContractError,
    require_array,
    require_int,
    require_schema_version,
    require_string_array,
    require_text,
    strict_object,
)


@dataclass(frozen=True, slots=True)
class GroundTruthTarget:
    target_id: str
    label: str
    position: GeoPoint
    behavior: str

    def __post_init__(self) -> None:
        for name in ("target_id", "label", "behavior"):
            object.__setattr__(self, name, require_text(getattr(self, name), name))
        if not isinstance(self.position, GeoPoint):
            raise AutonomyContractError("ground truth target position must be GeoPoint")

    @classmethod
    def from_mapping(cls, raw: object) -> GroundTruthTarget:
        value = strict_object(
            raw,
            name="GroundTruthTarget",
            required=frozenset({"target_id", "label", "position", "behavior"}),
        )
        return cls(
            target_id=require_text(value["target_id"], "target_id"),
            label=require_text(value["label"], "label"),
            position=GeoPoint.from_mapping(value["position"]),
            behavior=require_text(value["behavior"], "behavior"),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "label": self.label,
            "position": self.position.to_mapping(),
            "behavior": self.behavior,
        }


@dataclass(frozen=True, slots=True)
class ScenarioTruth:
    scenario_id: str
    scenario_version: int
    targets: tuple[GroundTruthTarget, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", require_schema_version(self.schema_version))
        object.__setattr__(
            self, "scenario_id", require_text(self.scenario_id, "scenario_id")
        )
        object.__setattr__(
            self,
            "scenario_version",
            require_int(self.scenario_version, "scenario_version", minimum=1),
        )
        if not isinstance(self.targets, Sequence) or isinstance(
            self.targets, (str, bytes, bytearray)
        ):
            raise AutonomyContractError("targets must be an array")
        targets = tuple(self.targets)
        if not targets or any(not isinstance(item, GroundTruthTarget) for item in targets):
            raise AutonomyContractError(
                "scenario truth targets must contain GroundTruthTarget values"
            )
        object.__setattr__(self, "targets", targets)
        target_ids = tuple(target.target_id for target in targets)
        if len(target_ids) != len(set(target_ids)):
            raise AutonomyContractError("ground truth target_id values must be unique")

    @classmethod
    def from_mapping(cls, raw: object) -> ScenarioTruth:
        value = strict_object(
            raw,
            name="ScenarioTruth",
            required=frozenset(
                {"schema_version", "scenario_id", "scenario_version", "targets"}
            ),
        )
        return cls(
            scenario_id=require_text(value["scenario_id"], "scenario_id"),
            scenario_version=require_int(
                value["scenario_version"], "scenario_version", minimum=1
            ),
            targets=tuple(
                GroundTruthTarget.from_mapping(item)
                for item in require_array(value["targets"], "targets")
            ),
            schema_version=require_schema_version(value["schema_version"]),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "scenario_id": self.scenario_id,
            "scenario_version": self.scenario_version,
            "targets": [item.to_mapping() for item in self.targets],
        }


@dataclass(frozen=True, slots=True)
class ScenarioExpectations:
    scenario_id: str
    scenario_version: int
    allowed_terminal_states: tuple[str, ...]
    maximum_geofence_violations: int
    maximum_false_geolocations: int
    maximum_unauthorized_commands: int
    schema_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", require_schema_version(self.schema_version))
        object.__setattr__(
            self, "scenario_id", require_text(self.scenario_id, "scenario_id")
        )
        object.__setattr__(
            self,
            "scenario_version",
            require_int(self.scenario_version, "scenario_version", minimum=1),
        )
        states = require_string_array(
            self.allowed_terminal_states, "allowed_terminal_states"
        )
        if not states:
            raise AutonomyContractError("allowed_terminal_states must not be empty")
        object.__setattr__(self, "allowed_terminal_states", states)
        for name in (
            "maximum_geofence_violations",
            "maximum_false_geolocations",
            "maximum_unauthorized_commands",
        ):
            object.__setattr__(
                self, name, require_int(getattr(self, name), name, minimum=0)
            )

    @classmethod
    def from_mapping(cls, raw: object) -> ScenarioExpectations:
        names = {
            "schema_version",
            "scenario_id",
            "scenario_version",
            "allowed_terminal_states",
            "maximum_geofence_violations",
            "maximum_false_geolocations",
            "maximum_unauthorized_commands",
        }
        value = strict_object(
            raw, name="ScenarioExpectations", required=frozenset(names)
        )
        return cls(
            scenario_id=require_text(value["scenario_id"], "scenario_id"),
            scenario_version=require_int(
                value["scenario_version"], "scenario_version", minimum=1
            ),
            allowed_terminal_states=require_string_array(
                value["allowed_terminal_states"], "allowed_terminal_states"
            ),
            maximum_geofence_violations=require_int(
                value["maximum_geofence_violations"],
                "maximum_geofence_violations",
                minimum=0,
            ),
            maximum_false_geolocations=require_int(
                value["maximum_false_geolocations"],
                "maximum_false_geolocations",
                minimum=0,
            ),
            maximum_unauthorized_commands=require_int(
                value["maximum_unauthorized_commands"],
                "maximum_unauthorized_commands",
                minimum=0,
            ),
            schema_version=require_schema_version(value["schema_version"]),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "scenario_id": self.scenario_id,
            "scenario_version": self.scenario_version,
            "allowed_terminal_states": list(self.allowed_terminal_states),
            "maximum_geofence_violations": self.maximum_geofence_violations,
            "maximum_false_geolocations": self.maximum_false_geolocations,
            "maximum_unauthorized_commands": self.maximum_unauthorized_commands,
        }


__all__ = ["GroundTruthTarget", "ScenarioExpectations", "ScenarioTruth"]
