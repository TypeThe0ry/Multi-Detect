"""Digital-twin manifests and runtime-safe scenario inputs."""

from .faults import FaultEvent, FaultKind
from .index import ScenarioChannelReference, ScenarioManifestIndex
from .manifest import ScenarioManifest, load_scenario_manifest
from .runtime_input import (
    RuntimeScenarioInput,
    ScenarioRuntimeInput,
    SensorRuntimeInput,
    VehicleRuntimeInput,
    VehicleSpawnPose,
    WorldRuntimeInput,
)
from .runtime_loader import load_runtime_scenario
from .truth import GroundTruthTarget, ScenarioExpectations, ScenarioTruth

__all__ = [
    "FaultEvent",
    "FaultKind",
    "RuntimeScenarioInput",
    "ScenarioChannelReference",
    "ScenarioExpectations",
    "ScenarioManifest",
    "ScenarioManifestIndex",
    "ScenarioRuntimeInput",
    "ScenarioTruth",
    "SensorRuntimeInput",
    "VehicleRuntimeInput",
    "VehicleSpawnPose",
    "WorldRuntimeInput",
    "GroundTruthTarget",
    "load_runtime_scenario",
    "load_scenario_manifest",
]
