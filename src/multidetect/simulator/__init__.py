"""3-D simulator orchestration and control-authority primitives.

The simulator is a separate runtime from REAL.  It reuses the same mission
and perception contracts, while this package owns process isolation,
deterministic scenario provenance and manual/AI authority arbitration.
"""

import importlib as _importlib

from ..mission_progress import (
    MAXIMUM_PROGRESS_ITEMS,
    MISSION_PROGRESS_SCHEMA_VERSION,
    MissionProgressConfig,
    MissionProgressError,
    MissionProgressPhase,
    MissionProgressSnapshot,
    MissionProgressTracker,
)
from .ai_executor import (
    DeterministicAiPolicy,
    DeterministicSimulatorAiPolicy,
    SimulatorAiAction,
    SimulatorAiCommand,
    SimulatorAiDecision,
    SimulatorAiExecutor,
    SimulatorAiExecutorConfig,
    SimulatorAiMode,
    SimulatorAiPolicy,
    SimulatorAiState,
    SimulatorTelemetry,
    SimulatorTelemetrySnapshot,
    TelemetrySnapshot,
)
from .closed_loop import (
    SimulatorClosedLoop,
    SimulatorClosedLoopError,
    SimulatorClosedLoopStep,
    SimulatorFlightModelConnection,
    SimulatorFlightModelMav,
)
from .control import (
    ControlAuthorityError,
    ControlAuthorityState,
    ControlSetpoint,
    SimulatorControlAuthority,
)
from .control_gateway import (
    SimulatorControlGateway,
    SimulatorControlGatewayConfig,
    SimulatorControlGatewayError,
    SimulatorControlGatewaySummary,
)
from .external_stack import (
    DEFAULT_EVIDENCE_NAME as EXTERNAL_STACK_DEFAULT_EVIDENCE_NAME,
)
from .external_stack import (
    DEFAULT_LOG_DIRECTORY_NAME as EXTERNAL_STACK_DEFAULT_LOG_DIRECTORY_NAME,
)
from .external_stack import (
    DEFAULT_PROCESS_ORDER as EXTERNAL_STACK_DEFAULT_PROCESS_ORDER,
)
from .external_stack import (
    ExternalProcess,
    ExternalProcessSpec,
    ExternalStackError,
    ExternalStackLauncher,
    ExternalStackPlan,
    ExternalStackValidationError,
    SimulatorExternalStackError,
    SimulatorExternalStackLauncher,
    SimulatorExternalStackPlan,
    build_external_launch_plan,
    build_external_stack_plan,
    plan_external_stack,
    prepare_external_stack,
)
from .flight_model import (
    MAV_CMD_COMPONENT_ARM_DISARM,
    MAV_CMD_DO_VTOL_TRANSITION,
    MAV_CMD_MISSION_START,
    MAV_CMD_NAV_LAND,
    MAV_CMD_NAV_RETURN_TO_LAUNCH,
    DeterministicFlightModel,
    FlightControlSetpoint,
    FlightMode,
    FlightModel,
    FlightModelConfig,
    FlightModelError,
    FlightTelemetrySnapshot,
    SimulatorFlightModel,
)
from .interactive_acceptance import run_acceptance as run_interactive_acceptance
from .interactive_router import (
    QgcControlMode,
    SimulatorInteractiveRouter,
    SimulatorInteractiveRouterConfig,
    SimulatorInteractiveRouterError,
    SimulatorInteractiveRouterSummary,
    TelemetrySink,
)
from .isolation import (
    SimulationIsolationError,
    ToolCapability,
    discover_simulator_tools,
    ensure_port_available,
    validate_simulation_endpoint,
)
from .live_ai_bridge import SimulatorTelemetryBridge
from .mavlink_control import (
    AiControlEnvelope,
    SimulatorControlReceipt,
    SimulatorMavlinkControlAdapter,
    SimulatorMavlinkControlConfig,
    SimulatorMavlinkControlError,
)
from .orchestrator import (
    SimulatorOrchestrator,
    SimulatorOrchestratorError,
    SimulatorProcessSpec,
    SimulatorRunResult,
)
from .perception_bridge import (
    SimulatorAiPerceptionBridge,
    SimulatorPerceptionBridge,
    SimulatorPerceptionFeedResult,
    SimulatorPerceptionObservationFeed,
)
from .px4_launch import (
    DEFAULT_GENERATED_RCS_NAME as PX4_DEFAULT_GENERATED_RCS_NAME,
)
from .px4_launch import (
    DEFAULT_JOB_DIRECTORY_NAME as PX4_DEFAULT_JOB_DIRECTORY_NAME,
)
from .px4_launch import (
    DEFAULT_OVERLAY_DIRECTORY_NAME as PX4_DEFAULT_OVERLAY_DIRECTORY_NAME,
)
from .px4_launch import (
    DEFAULT_PATCHED_RC_NAME as PX4_DEFAULT_PATCHED_RC_NAME,
)
from .px4_launch import (
    DEFAULT_RC_MAVLINK_RELATIVE as PX4_DEFAULT_RC_MAVLINK_RELATIVE,
)
from .px4_launch import (
    DEFAULT_RC_S_RELATIVE as PX4_DEFAULT_RC_S_RELATIVE,
)
from .px4_launch import (
    DEFAULT_WRAPPER_NAME as PX4_DEFAULT_WRAPPER_NAME,
)
from .px4_launch import (
    PX4LaunchError,
    PX4LaunchPlan,
    Px4LaunchPlan,
    SimulatorPX4LaunchError,
    SimulatorPx4LaunchError,
    SimulatorPX4LaunchPlan,
    SimulatorPx4LaunchPlan,
    build_px4_launch_plan,
    create_px4_launch_plan,
    patch_rc_mavlink,
    patch_rcS,
    patch_rcs,
    plan_px4_launch,
    prepare_px4_launch,
    verify_px4_launch_plan,
)
from .readiness import (
    DEFAULT_HEARTBEAT_POLL_INTERVAL_S,
    DEFAULT_HEARTBEAT_TIMEOUT_S,
    READINESS_SCHEMA_VERSION,
    HeartbeatObservation,
    ProcessReadiness,
    ProcessReadinessProbe,
    ReadinessResult,
    SimulatorHeartbeatObservation,
    SimulatorHeartbeatObserver,
    SimulatorProcessReadiness,
    SimulatorProcessReadinessProbe,
    SimulatorReadinessError,
    SimulatorReadinessProbe,
    SimulatorReadinessResult,
    parse_readiness_endpoint,
    probe_simulator_readiness,
)
from .runtime_preflight import (
    DOCKER_DIAGNOSTICS_SCHEMA_VERSION,
    EXTERNAL_RUNTIME_CONTRACT_SCHEMA_VERSION,
    RUNTIME_ASSET_PREFLIGHT_SCHEMA_VERSION,
    RUNTIME_PREFLIGHT_SCHEMA_VERSION,
    build_runtime_preflight_document,
    write_runtime_preflight_document,
)
from .safety_evidence import (
    DEFAULT_OUTPUT as SAFETY_EVIDENCE_DEFAULT_OUTPUT,
)
from .safety_evidence import (
    SimulatorSafetyEvidenceError,
)
from .safety_evidence import (
    run_acceptance as run_safety_acceptance,
)
from .safety_evidence import (
    verify_evidence as verify_safety_evidence,
)
from .scenario_matrix import (
    DEFAULT_SCENARIO_DIRECTORY,
    SCENARIO_MATRIX_SCHEMA_VERSION,
    SimulatorScenarioMatrixError,
    discover_scenario_manifests,
    run_scenario_matrix,
    verify_scenario_matrix_document,
)
from .scenario_matrix import verify_file as verify_scenario_matrix_file
from .scenario_runner import (
    ManualProvider,
    ObservationProvider,
    SimulatorScenarioRunner,
    SimulatorScenarioRunnerConfig,
    SimulatorScenarioRunnerError,
    SimulatorScenarioRunResult,
)
from .stack import (
    BuiltinWorldReference,
    SimulatorModelAssetBundle,
    SimulatorStackAsset,
    SimulatorStackAssets,
    SimulatorStackError,
    SimulatorStackIsolationError,
    SimulatorStackPlan,
    SimulatorStackPorts,
    StackAsset,
    StackPlan,
    StackPlanError,
    StackPorts,
    build_stack_plan,
    parse_builtin_world_uri,
    read_world_coordinates,
    resolve_repository_model_asset_bundle,
    resolve_simulator_assets,
    stack_plan_from_scenario,
)

_LAZY_SCENE_STATE_EXPORTS = frozenset(
    {
        "SCENE_SNAPSHOT_SCHEMA_VERSION",
        "SCENE_STREAM_EVENT",
        "SCENE_STREAM_SCHEMA_VERSION",
        "SimulatorSceneSnapshot",
        "SimulatorSceneStream",
        "scene_stream_from_result",
        "verify_scene_stream_file",
        "verify_scene_stream_main",
        "SCENE_STREAM_SET_EVENT",
        "SCENE_STREAM_SET_SCHEMA_VERSION",
        "SimulatorSceneStreamSetError",
        "build_scene_stream_set_binding",
        "verify_scene_stream_set",
        "verify_scene_stream_set_file",
        "verify_scene_stream_set_main",
    }
)


def __getattr__(name: str) -> object:
    """Load scene-state exports only when a caller requests them.

    Keeping this module lazy avoids importing ``scene_state`` before
    ``python -m multidetect.simulator.scene_state`` executes its module body,
    which otherwise produces a misleading ``runpy`` warning.
    """

    if name in _LAZY_SCENE_STATE_EXPORTS:
        module_name = (
            "scene_stream_set"
            if name
            in {
                "SCENE_STREAM_SET_EVENT",
                "SCENE_STREAM_SET_SCHEMA_VERSION",
                "SimulatorSceneStreamSetError",
                "build_scene_stream_set_binding",
                "verify_scene_stream_set",
                "verify_scene_stream_set_file",
                "verify_scene_stream_set_main",
            }
            else "scene_state"
        )
        module = _importlib.import_module(f"{__name__}.{module_name}")
        source_name = {
            "verify_scene_stream_file": "verify_file",
            "verify_scene_stream_main": "verify_main",
            "SCENE_STREAM_SET_EVENT": "EVENT",
            "SCENE_STREAM_SET_SCHEMA_VERSION": "SCHEMA_VERSION",
            "build_scene_stream_set_binding": "build_binding",
            "verify_scene_stream_set": "verify_binding",
            "verify_scene_stream_set_file": "verify_binding_file",
            "verify_scene_stream_set_main": "main",
        }.get(name, name)
        value = getattr(module, source_name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ControlAuthorityError",
    "ControlAuthorityState",
    "ControlSetpoint",
    "SimulatorControlAuthority",
    "SimulatorControlGateway",
    "SimulatorControlGatewayConfig",
    "SimulatorControlGatewayError",
    "SimulatorControlGatewaySummary",
    "DeterministicAiPolicy",
    "DeterministicSimulatorAiPolicy",
    "SimulatorAiAction",
    "SimulatorAiCommand",
    "SimulatorAiDecision",
    "SimulatorAiExecutor",
    "SimulatorAiExecutorConfig",
    "SimulatorAiMode",
    "SimulatorAiPolicy",
    "SimulatorAiState",
    "SimulatorTelemetry",
    "SimulatorTelemetrySnapshot",
    "TelemetrySnapshot",
    "SimulatorInteractiveRouter",
    "QgcControlMode",
    "SimulatorInteractiveRouterConfig",
    "SimulatorInteractiveRouterError",
    "SimulatorInteractiveRouterSummary",
    "TelemetrySink",
    "run_interactive_acceptance",
    "SimulationIsolationError",
    "ToolCapability",
    "discover_simulator_tools",
    "ensure_port_available",
    "validate_simulation_endpoint",
    "AiControlEnvelope",
    "SimulatorControlReceipt",
    "SimulatorMavlinkControlAdapter",
    "SimulatorMavlinkControlConfig",
    "SimulatorMavlinkControlError",
    "SimulatorTelemetryBridge",
    "SimulatorAiPerceptionBridge",
    "SimulatorPerceptionBridge",
    "SimulatorPerceptionFeedResult",
    "SimulatorPerceptionObservationFeed",
    "PX4_DEFAULT_JOB_DIRECTORY_NAME",
    "PX4_DEFAULT_GENERATED_RCS_NAME",
    "PX4_DEFAULT_OVERLAY_DIRECTORY_NAME",
    "PX4_DEFAULT_PATCHED_RC_NAME",
    "PX4_DEFAULT_RC_MAVLINK_RELATIVE",
    "PX4_DEFAULT_RC_S_RELATIVE",
    "PX4_DEFAULT_WRAPPER_NAME",
    "PX4LaunchPlan",
    "PX4LaunchError",
    "Px4LaunchPlan",
    "SimulatorPX4LaunchError",
    "SimulatorPX4LaunchPlan",
    "SimulatorPx4LaunchError",
    "SimulatorPx4LaunchPlan",
    "build_px4_launch_plan",
    "create_px4_launch_plan",
    "patch_rcS",
    "patch_rcs",
    "patch_rc_mavlink",
    "plan_px4_launch",
    "prepare_px4_launch",
    "verify_px4_launch_plan",
    "READINESS_SCHEMA_VERSION",
    "DEFAULT_HEARTBEAT_TIMEOUT_S",
    "DEFAULT_HEARTBEAT_POLL_INTERVAL_S",
    "SimulatorReadinessError",
    "parse_readiness_endpoint",
    "SimulatorProcessReadiness",
    "ProcessReadiness",
    "SimulatorHeartbeatObservation",
    "HeartbeatObservation",
    "SimulatorHeartbeatObserver",
    "SimulatorReadinessResult",
    "ReadinessResult",
    "SimulatorReadinessProbe",
    "ProcessReadinessProbe",
    "SimulatorProcessReadinessProbe",
    "probe_simulator_readiness",
    "RUNTIME_PREFLIGHT_SCHEMA_VERSION",
    "RUNTIME_ASSET_PREFLIGHT_SCHEMA_VERSION",
    "EXTERNAL_RUNTIME_CONTRACT_SCHEMA_VERSION",
    "build_runtime_preflight_document",
    "DOCKER_DIAGNOSTICS_SCHEMA_VERSION",
    "write_runtime_preflight_document",
    "EXTERNAL_STACK_DEFAULT_EVIDENCE_NAME",
    "EXTERNAL_STACK_DEFAULT_LOG_DIRECTORY_NAME",
    "EXTERNAL_STACK_DEFAULT_PROCESS_ORDER",
    "ExternalProcess",
    "ExternalProcessSpec",
    "ExternalStackError",
    "ExternalStackLauncher",
    "ExternalStackPlan",
    "ExternalStackValidationError",
    "SimulatorExternalStackError",
    "SimulatorExternalStackLauncher",
    "SimulatorExternalStackPlan",
    "build_external_launch_plan",
    "build_external_stack_plan",
    "plan_external_stack",
    "prepare_external_stack",
    "SimulatorOrchestrator",
    "SimulatorOrchestratorError",
    "SimulatorRunResult",
    "SimulatorProcessSpec",
    "DeterministicFlightModel",
    "FlightControlSetpoint",
    "FlightMode",
    "FlightModel",
    "FlightModelConfig",
    "FlightModelError",
    "FlightTelemetrySnapshot",
    "MAV_CMD_COMPONENT_ARM_DISARM",
    "MAV_CMD_DO_VTOL_TRANSITION",
    "MAV_CMD_MISSION_START",
    "MAV_CMD_NAV_LAND",
    "MAV_CMD_NAV_RETURN_TO_LAUNCH",
    "SimulatorFlightModel",
    "SimulatorClosedLoop",
    "SimulatorClosedLoopError",
    "SimulatorClosedLoopStep",
    "SimulatorFlightModelConnection",
    "SimulatorFlightModelMav",
    "MAXIMUM_PROGRESS_ITEMS",
    "MISSION_PROGRESS_SCHEMA_VERSION",
    "MissionProgressConfig",
    "MissionProgressError",
    "MissionProgressPhase",
    "MissionProgressSnapshot",
    "MissionProgressTracker",
    "ManualProvider",
    "ObservationProvider",
    "SimulatorScenarioRunResult",
    "SimulatorScenarioRunner",
    "SimulatorScenarioRunnerConfig",
    "SimulatorScenarioRunnerError",
    "SCENE_SNAPSHOT_SCHEMA_VERSION",
    "SCENE_STREAM_EVENT",
    "SCENE_STREAM_SCHEMA_VERSION",
    "SimulatorSceneSnapshot",
    "SimulatorSceneStream",
    "scene_stream_from_result",
    "verify_scene_stream_file",
    "verify_scene_stream_main",
    "SCENE_STREAM_SET_EVENT",
    "SCENE_STREAM_SET_SCHEMA_VERSION",
    "SimulatorSceneStreamSetError",
    "build_scene_stream_set_binding",
    "verify_scene_stream_set",
    "verify_scene_stream_set_file",
    "verify_scene_stream_set_main",
    "DEFAULT_SCENARIO_DIRECTORY",
    "SCENARIO_MATRIX_SCHEMA_VERSION",
    "SimulatorScenarioMatrixError",
    "discover_scenario_manifests",
    "run_scenario_matrix",
    "verify_scenario_matrix_file",
    "verify_scenario_matrix_document",
    "SAFETY_EVIDENCE_DEFAULT_OUTPUT",
    "SimulatorSafetyEvidenceError",
    "run_safety_acceptance",
    "verify_safety_evidence",
    "BuiltinWorldReference",
    "SimulatorModelAssetBundle",
    "SimulatorStackAsset",
    "SimulatorStackAssets",
    "SimulatorStackError",
    "SimulatorStackIsolationError",
    "SimulatorStackPlan",
    "SimulatorStackPorts",
    "StackAsset",
    "StackPlan",
    "StackPlanError",
    "StackPorts",
    "build_stack_plan",
    "parse_builtin_world_uri",
    "read_world_coordinates",
    "resolve_repository_model_asset_bundle",
    "resolve_simulator_assets",
    "stack_plan_from_scenario",
]
