"""Multi-Detect safety-first mission orchestration prototype.

The package root deliberately keeps its import surface light.  A few public
helpers (fixed-wing acceptance, runtime launchers and target-event replay)
pull in optional computer-vision/transport dependencies.  Importing a small
tool such as the release-gate evaluator must not require those optional
packages to be installed, so those exports are resolved lazily through
``__getattr__``.  Existing ``from multidetect import Name`` callers keep the
same API while command-line wrappers can run from a clean, minimal checkout.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from .config import MissionConfig
from .domain import Detection, FrameObservation, VehicleTelemetry
from .product_version import PRODUCT_VERSION, ProductVersion

# ``module`` is relative to this package; ``attribute`` is the public symbol
# exported by that module.  Keep this table explicit so importing the package
# never silently discovers arbitrary modules or optional dependencies.
_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    # Keep the release-gate evaluator lazy as well.  Besides keeping the root
    # import surface small, this prevents ``python -m`` execution of one of
    # the evidence modules from pre-importing that same module through the
    # package initializer (which would trigger runpy's duplicate-module
    # warning and fail under ``-W error``).
    "PARITY_FIELDS": (".release_gate", "PARITY_FIELDS"),
    "RELEASE_GATE_SCHEMA_VERSION": (".release_gate", "RELEASE_GATE_SCHEMA_VERSION"),
    "REQUIRED_MODES": (".release_gate", "REQUIRED_MODES"),
    "evaluate_release_gate": (".release_gate", "evaluate_release_gate"),
    "ActuatorOutput": (".actuator_safety", "ActuatorOutput"),
    "ActuatorSafetyConfig": (".actuator_safety", "ActuatorSafetyConfig"),
    "ActuatorSafetyError": (".actuator_safety", "ActuatorSafetyError"),
    "ActuatorSafetySession": (".actuator_safety", "ActuatorSafetySession"),
    "ActuatorSafetyState": (".actuator_safety", "ActuatorSafetyState"),
    "ServoChannelSpec": (".actuator_safety", "ServoChannelSpec"),
    "run_actuator_safety_software_acceptance": (
        ".actuator_safety",
        "run_actuator_safety_software_acceptance",
    ),
    "verify_actuator_safety_evidence": (
        ".actuator_safety",
        "verify_actuator_safety_evidence",
    ),
    "run_fixed_wing_software_acceptance": (
        ".fixed_wing_acceptance",
        "run_fixed_wing_software_acceptance",
    ),
    "ProfileBoundLiveBackend": (".runtime_launcher", "ProfileBoundLiveBackend"),
    "ProfileBoundRuntimeConfig": (".runtime_launcher", "ProfileBoundRuntimeConfig"),
    "ProfileBoundRuntimeLauncher": (".runtime_launcher", "ProfileBoundRuntimeLauncher"),
    "build_pixhawk_runtime_identity": (".runtime_launcher", "build_pixhawk_runtime_identity"),
    "derive_endpoint_binding_hash": (".runtime_launcher", "derive_endpoint_binding_hash"),
    "derive_signing_identity_hash": (".runtime_launcher", "derive_signing_identity_hash"),
    "derive_vehicle_identity_hash": (".runtime_launcher", "derive_vehicle_identity_hash"),
    "launch_profile_bound_runtime": (".runtime_launcher", "launch_profile_bound_runtime"),
    "RuntimeIdentity": (".runtime_profiles", "RuntimeIdentity"),
    "RuntimeProfileDocumentError": (".runtime_profiles", "RuntimeProfileDocumentError"),
    "RuntimeProfileLoadResult": (".runtime_profiles", "RuntimeProfileLoadResult"),
    "load_runtime_profile_document": (".runtime_profiles", "load_runtime_profile_document"),
    "sign_runtime_profile_document": (".runtime_profiles", "sign_runtime_profile_document"),
    "TargetEvent": (".target_event_replay", "TargetEvent"),
    "TargetEventInput": (".target_event_replay", "TargetEventInput"),
    "TargetEventReplayBuilder": (".target_event_replay", "TargetEventReplayBuilder"),
    "TargetEventReplayBundle": (".target_event_replay", "TargetEventReplayBundle"),
    "TargetGpsEvidence": (".target_event_replay", "TargetGpsEvidence"),
    "TargetMediaInput": (".target_event_replay", "TargetMediaInput"),
    "TargetMediaReference": (".target_event_replay", "TargetMediaReference"),
    "TargetRangeEvidence": (".target_event_replay", "TargetRangeEvidence"),
    "load_target_event_replay_manifest": (
        ".target_event_replay",
        "load_target_event_replay_manifest",
    ),
    "capture_field_session": (".field_session_capture", "capture_field_session"),
    "verify_field_session_capture": (
        ".field_session_capture",
        "verify_field_session_capture",
    ),
    "evaluate_replay_set": (".replay_set_evaluation", "evaluate_replay_set"),
    "RuntimeReportBindingError": (
        ".runtime_report_binding",
        "RuntimeReportBindingError",
    ),
    "verify_runtime_report_binding": (
        ".runtime_report_binding",
        "verify_runtime_report_binding",
    ),
    "check_replay_set_readiness": (
        ".replay_set_readiness",
        "check_replay_set_readiness",
    ),
    "prepare_deployment_replay_set": (
        ".replay_set_intake",
        "prepare_deployment_replay_set",
    ),
    "verify_deployment_replay_intake": (
        ".replay_set_intake",
        "verify_deployment_replay_intake",
    ),
    "create_replay_annotation_workspace": (
        ".replay_annotation_workspace",
        "create_replay_annotation_workspace",
    ),
    "inspect_replay_annotation_workspace": (
        ".replay_annotation_workspace",
        "inspect_replay_annotation_workspace",
    ),
    "finalize_replay_annotation_workspace": (
        ".replay_annotation_workspace",
        "finalize_replay_annotation_workspace",
    ),
    "verify_finalized_replay_annotations": (
        ".replay_annotation_workspace",
        "verify_finalized_replay_annotations",
    ),
    "run_replay_predictions": (
        ".replay_prediction_run",
        "run_replay_predictions",
    ),
    "verify_replay_predictions": (
        ".replay_prediction_run",
        "verify_replay_predictions",
    ),
    "ReplaySetCalibrationError": (
        ".replay_set_calibration",
        "ReplaySetCalibrationError",
    ),
    "calibrate_replay_set_thresholds": (
        ".replay_set_calibration",
        "calibrate_replay_set_thresholds",
    ),
    "verify_replay_set_calibration_report": (
        ".replay_set_calibration",
        "verify_replay_set_calibration_report",
    ),
    "create_video_replay_recording": (
        ".video_replay_recording",
        "create_video_replay_recording",
    ),
    "create_rtsp_evidence_replay_recording": (
        ".rtsp_evidence_replay",
        "create_rtsp_evidence_replay_recording",
    ),
    "create_evidence_transfer_inventory": (
        ".evidence_bundle_transfer",
        "create_evidence_transfer_inventory",
    ),
    "verify_evidence_bundle_transfer": (
        ".evidence_bundle_transfer",
        "verify_evidence_bundle_transfer",
    ),
    "verify_jetson_evidence_package": (
        ".jetson_evidence_package",
        "verify_jetson_evidence_package",
    ),
    "verify_jetson_evidence_installation": (
        ".jetson_evidence_install",
        "verify_jetson_evidence_installation",
    ),
    "join_raw_replay_telemetry": (
        ".replay_telemetry_join",
        "join_raw_replay_telemetry",
    ),
    "create_gnss_replay_sidecar": (
        ".gnss_capture_sidecar",
        "create_gnss_replay_sidecar",
    ),
    "discover_field_link_hosts": (
        ".field_link_preflight",
        "discover_field_link_hosts",
    ),
    "verify_field_link_discovery": (
        ".field_link_preflight",
        "verify_field_link_discovery",
    ),
    "verify_field_link_interface_diagnostic": (
        ".field_link_preflight",
        "verify_field_link_interface_diagnostic",
    ),
    "verify_field_link_report": (".field_link_preflight", "verify_field_link_report"),
    "verify_field_link_address_state": (
        ".field_link_address_state",
        "verify_field_link_address_state",
    ),
    "GeolocationSurveyResult": (".geolocation_survey", "GeolocationSurveyResult"),
    "build_accuracy_report_from_target_event_replay": (
        ".geolocation_survey",
        "build_accuracy_report_from_target_event_replay",
    ),
    "load_survey_truth": (".geolocation_survey", "load_survey_truth"),
    "GeolocationSurveyTemplateResult": (
        ".geolocation_survey_template",
        "GeolocationSurveyTemplateResult",
    ),
    "build_geolocation_survey_template": (
        ".geolocation_survey_template",
        "build_geolocation_survey_template",
    ),
    "write_geolocation_survey_template": (
        ".geolocation_survey_template",
        "write_geolocation_survey_template",
    ),
    "GnssCaptureError": (".gnss_capture", "GnssCaptureError"),
    "capture_gnss_samples": (".gnss_capture", "capture_gnss_samples"),
    "verify_gnss_capture_report": (
        ".gnss_capture",
        "verify_gnss_capture_report",
    ),
    "write_gnss_capture_report": (
        ".gnss_capture",
        "write_gnss_capture_report",
    ),
    "DurableMissionJournal": (".mission_journal", "DurableMissionJournal"),
    "MissionJournalCorruptionError": (
        ".mission_journal",
        "MissionJournalCorruptionError",
    ),
    "MissionJournalError": (".mission_journal", "MissionJournalError"),
    "MissionJournalIdentity": (".mission_journal", "MissionJournalIdentity"),
    "MissionJournalRecord": (".mission_journal", "MissionJournalRecord"),
    "MissionJournalRecovery": (".mission_journal", "MissionJournalRecovery"),
    "MissionJournalRecoveryRequired": (
        ".mission_journal",
        "MissionJournalRecoveryRequired",
    ),
    "MissionResumePlan": (".mission_journal", "MissionResumePlan"),
    "PersistentPx4MissionTransaction": (
        ".mission_journal",
        "PersistentPx4MissionTransaction",
    ),
    "MissionTransactionJournal": (".mission_journal", "MissionTransactionJournal"),
    "PersistentMissionTransaction": (".mission_journal", "PersistentMissionTransaction"),
    "Px4MissionAcceptanceError": (
        ".px4_mission_acceptance",
        "Px4MissionAcceptanceError",
    ),
    "run_px4_mission_transaction_acceptance": (
        ".px4_mission_acceptance",
        "run_px4_mission_transaction_acceptance",
    ),
    "verify_px4_mission_transaction_acceptance": (
        ".px4_mission_acceptance",
        "verify_px4_mission_transaction_acceptance",
    ),
    "ReleaseModeEvidenceError": (".release_mode_evidence", "ReleaseModeEvidenceError"),
    "build_release_mode_evidence": (
        ".release_mode_evidence",
        "build_release_mode_evidence",
    ),
    "write_release_mode_evidence": (
        ".release_mode_evidence",
        "write_release_mode_evidence",
    ),
    "CombinedSimulatorEvidenceError": (
        ".simulator_combined_evidence",
        "CombinedSimulatorEvidenceError",
    ),
    "verify_combined_sim_evidence": (
        ".simulator_combined_evidence",
        "verify_file",
    ),
    "check_field_evidence_bundle": (
        ".field_evidence_bundle",
        "check_field_evidence_bundle",
    ),
    "verify_field_evidence_bundle_report": (
        ".field_evidence_bundle",
        "verify_field_evidence_bundle_report",
    ),
    "FIELD_EVIDENCE_SCHEMA_VERSION": (
        ".field_evidence_bundle",
        "FIELD_EVIDENCE_SCHEMA_VERSION",
    ),
    "FIELD_EVIDENCE_EVIDENCE_TYPE": (
        ".field_evidence_bundle",
        "FIELD_EVIDENCE_EVIDENCE_TYPE",
    ),
    "FIELD_EVIDENCE_STRATA": (".field_evidence_bundle", "FIELD_EVIDENCE_STRATA"),
    "write_field_evidence_bundle": (
        ".field_evidence_bundle",
        "write_field_evidence_bundle",
    ),
    "CAMERA_MOUNT_SURVEY_SCHEMA_VERSION": (
        ".camera_mount_survey",
        "CAMERA_MOUNT_SURVEY_SCHEMA_VERSION",
    ),
    "CAMERA_MOUNT_SURVEY_EVIDENCE_TYPE": (
        ".camera_mount_survey",
        "CAMERA_MOUNT_SURVEY_EVIDENCE_TYPE",
    ),
    "CAMERA_MOUNT_SURVEY_FRAME": (
        ".camera_mount_survey",
        "CAMERA_MOUNT_SURVEY_FRAME",
    ),
    "CAMERA_MOUNT_MEASUREMENT_PREFLIGHT_EVIDENCE_TYPE": (
        ".camera_mount_survey",
        "CAMERA_MOUNT_MEASUREMENT_PREFLIGHT_EVIDENCE_TYPE",
    ),
    "check_camera_mount_measurements": (
        ".camera_mount_survey",
        "check_camera_mount_measurements",
    ),
    "create_camera_mount_equipment_calibration_record": (
        ".camera_mount_survey",
        "create_camera_mount_equipment_calibration_record",
    ),
    "create_camera_mount_survey": (
        ".camera_mount_survey",
        "create_camera_mount_survey",
    ),
    "verify_camera_mount_survey": (
        ".camera_mount_survey",
        "verify_camera_mount_survey",
    ),
    "write_camera_mount_survey": (
        ".camera_mount_survey",
        "write_camera_mount_survey",
    ),
    "write_camera_mount_measurement_template": (
        ".camera_mount_survey",
        "write_camera_mount_measurement_template",
    ),
    "write_camera_mount_equipment_calibration_record": (
        ".camera_mount_survey",
        "write_camera_mount_equipment_calibration_record",
    ),
    "PlannedMissionCompilation": (
        ".mission_planning",
        "PlannedMissionCompilation",
    ),
    "PLANNED_MISSION_COMPILATION_SCHEMA_VERSION": (
        ".mission_planning",
        "PLANNED_MISSION_COMPILATION_SCHEMA_VERSION",
    ),
    "compile_planned_task_mission": (
        ".mission_planning",
        "compile_planned_task_mission",
    ),
    "compile_planned_task_mission_from_runtime": (
        ".mission_planning",
        "compile_planned_task_mission_from_runtime",
    ),
    "parse_mission_compile_runtime": (
        ".mission_planning",
        "parse_mission_compile_runtime",
    ),
    "MISSION_COMPILE_RUNTIME_SCHEMA_VERSION": (
        ".mission_planning",
        "MISSION_COMPILE_RUNTIME_SCHEMA_VERSION",
    ),
    "MAXIMUM_PROGRESS_ITEMS": (".mission_progress", "MAXIMUM_PROGRESS_ITEMS"),
    "MISSION_PROGRESS_SCHEMA_VERSION": (
        ".mission_progress",
        "MISSION_PROGRESS_SCHEMA_VERSION",
    ),
    "MissionProgressConfig": (".mission_progress", "MissionProgressConfig"),
    "MissionProgressError": (".mission_progress", "MissionProgressError"),
    "MissionProgressPhase": (".mission_progress", "MissionProgressPhase"),
    "MissionProgressSnapshot": (".mission_progress", "MissionProgressSnapshot"),
    "MissionProgressTracker": (".mission_progress", "MissionProgressTracker"),
    "PX4_MISSION_FEEDBACK_CANONICAL_MAX_AGE_S": (
        ".px4_mission_feedback",
        "PX4_MISSION_FEEDBACK_CANONICAL_MAX_AGE_S",
    ),
    "PX4_MISSION_FEEDBACK_MAX_ITEMS": (
        ".px4_mission_feedback",
        "PX4_MISSION_FEEDBACK_MAX_ITEMS",
    ),
    "PX4_MISSION_FEEDBACK_SCHEMA_VERSION": (
        ".px4_mission_feedback",
        "PX4_MISSION_FEEDBACK_SCHEMA_VERSION",
    ),
    "Px4MissionFeedbackConfig": (
        ".px4_mission_feedback",
        "Px4MissionFeedbackConfig",
    ),
    "Px4MissionFeedbackError": (
        ".px4_mission_feedback",
        "Px4MissionFeedbackError",
    ),
    "Px4MissionFeedbackPhase": (
        ".px4_mission_feedback",
        "Px4MissionFeedbackPhase",
    ),
    "Px4MissionFeedbackSnapshot": (
        ".px4_mission_feedback",
        "Px4MissionFeedbackSnapshot",
    ),
    "Px4MissionFeedbackTracker": (
        ".px4_mission_feedback",
        "Px4MissionFeedbackTracker",
    ),
    "PX4_MISSION_FEEDBACK_BINDING_SCHEMA_VERSION": (
        ".px4_mission_feedback_binding",
        "PX4_MISSION_FEEDBACK_BINDING_SCHEMA_VERSION",
    ),
    "PX4_MISSION_FEEDBACK_REQUIRED_COMBINATIONS": (
        ".px4_mission_feedback_binding",
        "PX4_MISSION_FEEDBACK_REQUIRED_COMBINATIONS",
    ),
    "Px4MissionFeedbackBindingError": (
        ".px4_mission_feedback_binding",
        "Px4MissionFeedbackBindingError",
    ),
    "build_px4_mission_feedback_binding": (
        ".px4_mission_feedback_binding",
        "build_px4_mission_feedback_binding",
    ),
    "verify_px4_mission_feedback_binding": (
        ".px4_mission_feedback_binding",
        "verify_px4_mission_feedback_binding",
    ),
    "PX4_MISSION_FEEDBACK_RUNTIME_BINDING_SCHEMA_VERSION": (
        ".px4_mission_feedback_runtime",
        "PX4_MISSION_FEEDBACK_RUNTIME_BINDING_SCHEMA_VERSION",
    ),
    "Px4MissionFeedbackRuntimeBindingError": (
        ".px4_mission_feedback_runtime",
        "Px4MissionFeedbackRuntimeBindingError",
    ),
    "build_px4_mission_feedback_runtime_binding": (
        ".px4_mission_feedback_runtime",
        "build_px4_mission_feedback_runtime_binding",
    ),
    "verify_px4_mission_feedback_runtime_binding": (
        ".px4_mission_feedback_runtime",
        "verify_px4_mission_feedback_runtime_binding",
    ),
}


def __getattr__(name: str) -> Any:
    """Resolve optional public exports on first access."""

    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = target
    value = getattr(import_module(module_name, __name__), attribute_name)
    # Cache the value so subsequent access has normal module-attribute speed
    # and introspection sees the same object as a direct import.
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Include lazy public names in ``dir(multidetect)`` without importing them."""

    return sorted(set(globals()).union(globals().get("__all__", ())))


__all__ = [
    "ActuatorOutput",
    "ActuatorSafetyConfig",
    "ActuatorSafetyError",
    "ActuatorSafetySession",
    "ActuatorSafetyState",
    "Detection",
    "FrameObservation",
    "MissionConfig",
    "PRODUCT_VERSION",
    "ProductVersion",
    "PARITY_FIELDS",
    "RELEASE_GATE_SCHEMA_VERSION",
    "REQUIRED_MODES",
    "evaluate_release_gate",
    "RuntimeIdentity",
    "RuntimeProfileDocumentError",
    "RuntimeProfileLoadResult",
    "load_runtime_profile_document",
    "sign_runtime_profile_document",
    "ProfileBoundLiveBackend",
    "ProfileBoundRuntimeConfig",
    "ProfileBoundRuntimeLauncher",
    "build_pixhawk_runtime_identity",
    "derive_endpoint_binding_hash",
    "derive_signing_identity_hash",
    "derive_vehicle_identity_hash",
    "launch_profile_bound_runtime",
    "VehicleTelemetry",
    "ServoChannelSpec",
    "run_actuator_safety_software_acceptance",
    "verify_actuator_safety_evidence",
    "run_fixed_wing_software_acceptance",
    "TargetEvent",
    "TargetEventInput",
    "TargetEventReplayBuilder",
    "TargetEventReplayBundle",
    "TargetGpsEvidence",
    "TargetMediaInput",
    "TargetMediaReference",
    "TargetRangeEvidence",
    "load_target_event_replay_manifest",
    "capture_field_session",
    "verify_field_session_capture",
    "evaluate_replay_set",
    "RuntimeReportBindingError",
    "verify_runtime_report_binding",
    "check_replay_set_readiness",
    "prepare_deployment_replay_set",
    "verify_deployment_replay_intake",
    "create_replay_annotation_workspace",
    "inspect_replay_annotation_workspace",
    "finalize_replay_annotation_workspace",
    "verify_finalized_replay_annotations",
    "run_replay_predictions",
    "verify_replay_predictions",
    "ReplaySetCalibrationError",
    "calibrate_replay_set_thresholds",
    "verify_replay_set_calibration_report",
    "create_video_replay_recording",
    "create_rtsp_evidence_replay_recording",
    "join_raw_replay_telemetry",
    "create_gnss_replay_sidecar",
    "discover_field_link_hosts",
    "verify_field_link_discovery",
    "verify_field_link_interface_diagnostic",
    "verify_field_link_report",
    "verify_field_link_address_state",
    "GeolocationSurveyResult",
    "build_accuracy_report_from_target_event_replay",
    "load_survey_truth",
    "GeolocationSurveyTemplateResult",
    "build_geolocation_survey_template",
    "write_geolocation_survey_template",
    "GnssCaptureError",
    "capture_gnss_samples",
    "verify_gnss_capture_report",
    "write_gnss_capture_report",
    "DurableMissionJournal",
    "MissionJournalCorruptionError",
    "MissionJournalError",
    "MissionJournalIdentity",
    "MissionJournalRecord",
    "MissionJournalRecovery",
    "MissionJournalRecoveryRequired",
    "MissionResumePlan",
    "PersistentPx4MissionTransaction",
    "MissionTransactionJournal",
    "PersistentMissionTransaction",
    "Px4MissionAcceptanceError",
    "run_px4_mission_transaction_acceptance",
    "verify_px4_mission_transaction_acceptance",
    "ReleaseModeEvidenceError",
    "build_release_mode_evidence",
    "write_release_mode_evidence",
    "CombinedSimulatorEvidenceError",
    "verify_combined_sim_evidence",
    "FIELD_EVIDENCE_SCHEMA_VERSION",
    "FIELD_EVIDENCE_EVIDENCE_TYPE",
    "FIELD_EVIDENCE_STRATA",
    "check_field_evidence_bundle",
    "verify_field_evidence_bundle_report",
    "write_field_evidence_bundle",
    "CAMERA_MOUNT_SURVEY_SCHEMA_VERSION",
    "CAMERA_MOUNT_SURVEY_EVIDENCE_TYPE",
    "CAMERA_MOUNT_SURVEY_FRAME",
    "CAMERA_MOUNT_MEASUREMENT_PREFLIGHT_EVIDENCE_TYPE",
    "check_camera_mount_measurements",
    "create_camera_mount_equipment_calibration_record",
    "create_camera_mount_survey",
    "verify_camera_mount_survey",
    "write_camera_mount_survey",
    "write_camera_mount_measurement_template",
    "write_camera_mount_equipment_calibration_record",
    "PLANNED_MISSION_COMPILATION_SCHEMA_VERSION",
    "PlannedMissionCompilation",
    "compile_planned_task_mission",
    "compile_planned_task_mission_from_runtime",
    "parse_mission_compile_runtime",
    "MISSION_COMPILE_RUNTIME_SCHEMA_VERSION",
    "MAXIMUM_PROGRESS_ITEMS",
    "MISSION_PROGRESS_SCHEMA_VERSION",
    "MissionProgressConfig",
    "MissionProgressError",
    "MissionProgressPhase",
    "MissionProgressSnapshot",
    "MissionProgressTracker",
    "PX4_MISSION_FEEDBACK_CANONICAL_MAX_AGE_S",
    "PX4_MISSION_FEEDBACK_MAX_ITEMS",
    "PX4_MISSION_FEEDBACK_SCHEMA_VERSION",
    "Px4MissionFeedbackConfig",
    "Px4MissionFeedbackError",
    "Px4MissionFeedbackPhase",
    "Px4MissionFeedbackSnapshot",
    "Px4MissionFeedbackTracker",
    "PX4_MISSION_FEEDBACK_BINDING_SCHEMA_VERSION",
    "PX4_MISSION_FEEDBACK_REQUIRED_COMBINATIONS",
    "Px4MissionFeedbackBindingError",
    "build_px4_mission_feedback_binding",
    "verify_px4_mission_feedback_binding",
    "PX4_MISSION_FEEDBACK_RUNTIME_BINDING_SCHEMA_VERSION",
    "Px4MissionFeedbackRuntimeBindingError",
    "build_px4_mission_feedback_runtime_binding",
    "verify_px4_mission_feedback_runtime_binding",
]
__version__ = PRODUCT_VERSION.python
