"""SIM teacher collection primitives."""

from .executor import ExecutionResult, PymavlinkPrimitiveExecutor, SetpointStreamer
from .fw_executor import FixedWingVtolExecutor
from .isolation import SimulationIsolationError, assert_localhost, validate_local_endpoint
from .policies import (
    ExpertSearchPolicy,
    ExploreBounds,
    ExplorePolicy,
    HoverPolicy,
    SDFObstacle,
    load_sdf_obstacles,
    segment_clear_of_obstacles,
    target_in_fov,
)
from .primitives import ActionPrimitive, CompletionCriterion, body_to_ned, primitive_to_ned
from .recording import DatasetAuditError, EpisodeRecorder, audit_dataset
from .streams import (
    MavlinkReader,
    MavlinkState,
    SensorSample,
    SensorStreamReader,
    build_observation,
)

__all__ = [
    "ActionPrimitive",
    "CompletionCriterion",
    "body_to_ned",
    "primitive_to_ned",
    "ExploreBounds",
    "ExplorePolicy",
    "ExpertSearchPolicy",
    "HoverPolicy",
    "SDFObstacle",
    "load_sdf_obstacles",
    "segment_clear_of_obstacles",
    "target_in_fov",
    "EpisodeRecorder",
    "DatasetAuditError",
    "audit_dataset",
    "SimulationIsolationError",
    "validate_local_endpoint",
    "assert_localhost",
    "ExecutionResult",
    "PymavlinkPrimitiveExecutor",
    "SetpointStreamer",
    "FixedWingVtolExecutor",
    "SensorSample",
    "SensorStreamReader",
    "MavlinkState",
    "MavlinkReader",
    "build_observation",
]
