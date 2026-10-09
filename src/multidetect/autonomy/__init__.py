"""Platform-neutral autonomy contracts for Multi-Detect.

The perception adapter is exposed lazily.  It depends on the ranging module,
which itself imports the lightweight autonomy serialization contracts.  Eagerly
loading both modules while importing :mod:`multidetect.perception_factory`
creates a circular import before the public package has finished initializing.
"""

from importlib import import_module

# Import order is intentional: mission_planning imports the public autonomy
# names while onboarding imports mission_campaign, so moving onboarding above
# planner creates a circular import during package initialization.
# ruff: noqa: I001

from .contracts import (
    AutonomyContractError,
    ContextSnapshot,
    EpisodeArtifact,
    EpisodeBundle,
    EpisodeOutcome,
    ExecutableMission,
    GeoPoint,
    MissionCommand,
    MissionFrame,
    MissionItem,
    OperatorGrant,
    PlanAction,
    PlanStep,
    PlatformNeutralPlan,
    PromptEnvelope,
    ResolvedTaskSpec,
    TaskConstraints,
    TaskGeometry,
    TaskType,
    ValidationFinding,
    ValidationReport,
    ValidationVerdict,
)
from .planner import (
    PlannerError,
    PlannerPolicy,
    PlanningEstimate,
    compile_platform_neutral_plan,
    estimate_task,
    normalize_vehicle_class,
    plan_task,
)
from .policy_artifact import PolicyArtifact, load_policy_artifact
from .onboard_model_reference import (
    DEFAULT_ONBOARD_MODEL_REFERENCE,
    ONBOARD_MODEL_REFERENCE_CONTRACT,
    ONBOARD_MODEL_REFERENCE_SCHEMA_VERSION,
    OnboardModelReference,
    OnboardModelReferenceError,
    load_onboard_model_reference,
)
from .policy_contract import (
    PolicyAction,
    PolicyDecision,
    PolicyObservation,
    SharedTaskPolicy,
)
from .onboard_agent import (
    ModelBackedDecisionProvider,
    ONBOARD_MODEL_EVENT_KINDS,
    ONBOARD_MODEL_FEATURE_NAMES,
    ONBOARD_MODEL_LEGACY_FEATURE_NAMES,
    ONBOARD_MODEL_TASK_PHASES,
    OnboardAIExecutor,
    OnboardDecision,
    OnboardDecisionProvider,
    OnboardEventKind,
    OnboardFlightAction,
    OnboardFlightPlan,
    OnboardTaskAction,
    OnboardModelInput,
    OnboardModelOutput,
    OnboardModelProposal,
    OnboardModelRunner,
    OnboardResponse,
    OnboardRuntimeEvent,
)
from .onboard_adapter import (
    MissionTransactionDispatcher,
    OnboardMissionAdapter,
    OnboardMissionAdapterError,
    OnboardMissionEventLoop,
    OnboardMissionRevision,
    OnboardFlightPlanDispatch,
)
from .realtime_bridge import (
    MissionStartDispatcher,
    OnboardAutonomousSortie,
    OnboardAutonomousSortieStatus,
    OnboardPerceptionObservation,
    OnboardRealtimeModelBridge,
    ReturnRequestDispatcher,
    TaskReportDispatcher,
)
from .onboard_task_state import (
    MIN_TARGET_DETECTOR_CONFIDENCE,
    MIN_TARGET_MATCH_SCORE,
    OnboardTaskReceipt,
    OnboardTaskStateError,
    OnboardTaskStateMachine,
)
from .onboard_onnx import (
    ONBOARD_MODEL_ACTION_ORDER,
    ONBOARD_MODEL_RESPONSE_ORDER,
    ONBOARD_MODEL_TASK_ACTION_ORDER,
    OnboardOnnxModelError,
    OnboardOnnxModelRunner,
    allowed_task_actions_for_event,
    is_task_flight_compatible,
)
from .prompt_resolution import (
    PromptResolutionError,
    PromptTaskResolution,
    PromptTaskResolver,
    PromptTaskTemplate,
    runtime_sha256,
)
from .task_policy_runtime import TaskPolicyRuntimeBinding

__all__ = [
    "AutonomyContractError",
    "PlannerError",
    "PlannerPolicy",
    "PlanningEstimate",
    "ContextSnapshot",
    "EpisodeArtifact",
    "EpisodeBundle",
    "EpisodeOutcome",
    "ExecutableMission",
    "GeoPoint",
    "MissionCommand",
    "MissionFrame",
    "MissionItem",
    "OperatorGrant",
    "PlanAction",
    "PlanStep",
    "PlatformNeutralPlan",
    "PromptEnvelope",
    "ResolvedTaskSpec",
    "TaskConstraints",
    "TaskGeometry",
    "TaskType",
    "ValidationFinding",
    "ValidationReport",
    "ValidationVerdict",
    "compile_platform_neutral_plan",
    "estimate_task",
    "normalize_vehicle_class",
    "plan_task",
    "PolicyArtifact",
    "load_policy_artifact",
    "DEFAULT_ONBOARD_MODEL_REFERENCE",
    "ONBOARD_MODEL_REFERENCE_CONTRACT",
    "ONBOARD_MODEL_REFERENCE_SCHEMA_VERSION",
    "OnboardModelReference",
    "OnboardModelReferenceError",
    "load_onboard_model_reference",
    "PolicyAction",
    "PolicyDecision",
    "PolicyObservation",
    "SharedTaskPolicy",
    "OnboardAIExecutor",
    "OnboardDecision",
    "OnboardDecisionProvider",
    "OnboardEventKind",
    "OnboardFlightAction",
    "OnboardFlightPlan",
    "OnboardTaskAction",
    "OnboardModelInput",
    "OnboardModelOutput",
    "OnboardModelProposal",
    "OnboardModelRunner",
    "ModelBackedDecisionProvider",
    "ONBOARD_MODEL_EVENT_KINDS",
    "ONBOARD_MODEL_FEATURE_NAMES",
    "ONBOARD_MODEL_LEGACY_FEATURE_NAMES",
    "ONBOARD_MODEL_TASK_PHASES",
    "OnboardResponse",
    "OnboardRuntimeEvent",
    "OnboardMissionAdapter",
    "OnboardMissionAdapterError",
    "OnboardMissionEventLoop",
    "OnboardMissionRevision",
    "OnboardFlightPlanDispatch",
    "MissionTransactionDispatcher",
    "OnboardPerceptionObservation",
    "OnboardRealtimeModelBridge",
    "OnboardAutonomousSortie",
    "OnboardAutonomousSortieStatus",
    "MissionStartDispatcher",
    "ReturnRequestDispatcher",
    "TaskReportDispatcher",
    "FramePerceptionResult",
    "FrameCameraPose",
    "OnboardFramePerceptionAdapter",
    "OnboardTaskReceipt",
    "OnboardTaskStateError",
    "OnboardTaskStateMachine",
    "MIN_TARGET_DETECTOR_CONFIDENCE",
    "MIN_TARGET_MATCH_SCORE",
    "ONBOARD_MODEL_RESPONSE_ORDER",
    "ONBOARD_MODEL_ACTION_ORDER",
    "ONBOARD_MODEL_TASK_ACTION_ORDER",
    "OnboardOnnxModelError",
    "OnboardOnnxModelRunner",
    "allowed_task_actions_for_event",
    "is_task_flight_compatible",
    "PromptResolutionError",
    "PromptTaskResolution",
    "PromptTaskResolver",
    "PromptTaskTemplate",
    "runtime_sha256",
    "TaskPolicyRuntimeBinding",
]


_LAZY_EXPORTS = {
    "FrameCameraPose": ".perception_adapter",
    "FramePerceptionResult": ".perception_adapter",
    "OnboardFramePerceptionAdapter": ".perception_adapter",
}


def __getattr__(name: str) -> object:
    """Resolve perception exports only after the base contracts are loaded."""

    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value
