"""Onboard policy v2 contracts and truth-free state adapters."""

from .contracts import (
    PolicyAction,
    PolicyActionV2,
    PolicyContractError,
    StructuredSceneState,
    TaskSpec,
    TaskSpecV1,
)
from .model import (
    ACTION_KINDS,
    HISTORY_LENGTH,
    MEMORY_CHANNELS,
    MEMORY_SIZE,
    STATE_FEATURE_DIM,
    ONNXPolicyRunner,
    PolicyONNXRunner,
    PrimitivePolicy,
    export_onnx,
    state_to_vector,
    states_to_tensors,
)
from .perception_bridge import (
    COCO80_CLASS_NAMES,
    DetectorSpec,
    PerceptionBridge,
    bearing_from_pixel,
    robust_depth_median,
)
from .rollout import EpisodeMetrics, RolloutConfig, rollout_episode, run_rollout
from .search_memory import SearchMemoryGrid, SearchMemorySnapshot
from .state_builder import StateBuilderInput, StructuredSceneStateBuilder
from .vlm_grounding import (
    AttributeGroundingResult,
    AttributeGrounder,
    TaskSpecResolver,
    VlmGroundingError,
    VlmOutputError,
    VlmTimeoutError,
    deterministic_task_spec,
)

__all__ = [
    "PolicyAction",
    "PolicyActionV2",
    "PolicyContractError",
    "SearchMemoryGrid",
    "SearchMemorySnapshot",
    "COCO80_CLASS_NAMES",
    "DetectorSpec",
    "PerceptionBridge",
    "bearing_from_pixel",
    "robust_depth_median",
    "StateBuilderInput",
    "StructuredSceneState",
    "StructuredSceneStateBuilder",
    "TaskSpec",
    "TaskSpecV1",
    "ACTION_KINDS",
    "HISTORY_LENGTH",
    "MEMORY_CHANNELS",
    "MEMORY_SIZE",
    "ONNXPolicyRunner",
    "PolicyONNXRunner",
    "PrimitivePolicy",
    "STATE_FEATURE_DIM",
    "export_onnx",
    "state_to_vector",
    "states_to_tensors",
    "EpisodeMetrics",
    "RolloutConfig",
    "rollout_episode",
    "run_rollout",
    "AttributeGroundingResult",
    "AttributeGrounder",
    "TaskSpecResolver",
    "VlmGroundingError",
    "VlmOutputError",
    "VlmTimeoutError",
    "deterministic_task_spec",
]
