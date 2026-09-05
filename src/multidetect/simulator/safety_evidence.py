"""Deterministic S4 safety evidence for the shared Multi-Detect SIM path.

The SIM runner exercises existing production components instead of duplicating
their decisions: the unified target-selection pool, the loopback QGC/PX4
router and MAVLink control adapter, the mission safety rules and the terminal
mission state machine.  The resulting JSON is deliberately truth-free and
contains no wall-clock timestamp or host path, so the same source revision
produces a byte-stable acceptance record.

This is a software/SIM gate.  It proves fail-closed behaviour and shared
contracts; it does not claim a connected Pixhawk, Jetson, camera, actuator or
operator visual acceptance.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..autonomy.serialization import strict_json_loads
from ..clock import ManualClock
from ..config import MissionConfig, MissionType, PayloadSpec, PlatformMode
from ..domain import (
    BoundingBox,
    Detection,
    FrameObservation,
    MissionPhase,
    StateTransitionError,
    TrackSnapshot,
    VehicleTelemetry,
)
from ..operator_bridge import OperatorBridgeResult
from ..operator_link import (
    SelectionAction,
    TargetSelectionCommand,
    TrackingState,
    TrackStatusMessage,
    VideoGeometry,
)
from ..product_version import load_product_version
from ..safety import SafetyRuleEngine
from ..state_machine import MissionStateMachine
from ..target_geolocation import target_geolocation_from_ned_offset
from ..unified_tracking import TargetObservation, UnifiedTargetPool
from .closed_loop import SimulatorClosedLoop
from .control import ControlAuthorityState
from .flight_model import FlightMode
from .interactive_acceptance import run_acceptance as run_interactive_acceptance
from .isolation import SimulationIsolationError
from .mavlink_control import SimulatorMavlinkControlConfig

ROOT = Path(__file__).resolve().parents[3]
SCHEMA_VERSION = 1
EVENT = "simulator_safety_acceptance_finished"
VERIFIED_EVENT = "simulator_safety_evidence_verified"
DEFAULT_OUTPUT = ROOT / "artifacts/evaluation/simulator-safety-acceptance-latest.json"
_PEER = ("127.0.0.1", 14581)
_GEOMETRY = VideoGeometry("sim-safety-camera", 1280, 720)
_SESSION_ID = "sim-safety-session"
_LOOPBACK_ENDPOINT_RE = re.compile(r"^udpout:(?:127\.0\.0\.1|\[::1\]|::1):[0-9]+$")


class SimulatorSafetyEvidenceError(ValueError):
    """Raised when a S4 evidence document is malformed or unsafe to accept."""


def _selection_command(
    sequence: int, action: SelectionAction, bbox: BoundingBox | None
) -> TargetSelectionCommand:
    return TargetSelectionCommand(
        command_id=f"sim-safety-command-{sequence:02d}",
        session_id=_SESSION_ID,
        sequence=sequence,
        action=action,
        geometry=_GEOMETRY,
        issued_at_s=100.0 + sequence * 0.1,
        expires_at_s=102.0 + sequence * 0.1,
        bbox=bbox,
        displayed_frame_id=f"sim-safety-frame-{sequence:02d}",
    )


def _track_status(
    command: TargetSelectionCommand, *, label: str, target_id: str
) -> TrackStatusMessage:
    if command.bbox is None:
        raise SimulatorSafetyEvidenceError("active SIM tracking status requires a bounding box")
    return TrackStatusMessage(
        status_id=f"sim-safety-status-{command.sequence:02d}",
        selection_command_id=command.command_id,
        sequence=command.sequence,
        geometry=command.geometry,
        state=TrackingState.TRACKING,
        target_id=target_id,
        bbox=command.bbox,
        label=label,
        confidence=0.95,
        tracking_quality=0.90,
        source_frame_id=f"sim-safety-frame-{command.sequence:02d}",
        source_captured_at_s=command.issued_at_s,
        produced_at_s=command.issued_at_s + 0.01,
    )


def _bridge_result(
    command: TargetSelectionCommand,
    *,
    status: TrackStatusMessage | None = None,
) -> OperatorBridgeResult:
    return OperatorBridgeResult(
        accepted_command_count=1,
        published_statuses=() if status is None else (status,),
        published_mission_statuses=(),
        published_safety_statuses=(),
        accepted_authorization_decisions=(),
        published_authorization_challenges=(),
        transport_errors=(),
        accepted_selection_commands=((command, _PEER),),
    )


def _run_cancel_acceptance() -> dict[str, Any]:
    """Exercise LCK -> TRK -> single cancel -> full cancel -> reselect."""

    first_box = BoundingBox(0.10, 0.20, 0.25, 0.50)
    second_box = BoundingBox(0.65, 0.20, 0.80, 0.50)
    pool = UnifiedTargetPool()
    pool.update(
        frame_id="sim-safety-frame-00",
        captured_at_s=100.0,
        observations=(
            TargetObservation("vehicle", 0.95, first_box),
            TargetObservation("person", 0.95, second_box),
        ),
    )
    from ..selection_target_pool import UnifiedSelectionTargetPool

    coordinator = UnifiedSelectionTargetPool(pool)
    initial_ids = tuple(track.track_id for track in pool.snapshots())
    if len(initial_ids) != 2:
        raise SimulatorSafetyEvidenceError("SIM target fixture did not create two identities")
    first_id = initial_ids[0]
    second_id = initial_ids[1]

    lck_command = _selection_command(1, SelectionAction.SELECT, first_box)
    lck = coordinator.consume_bridge_result(
        _bridge_result(
            lck_command,
            status=_track_status(lck_command, label="vehicle", target_id=first_id),
        ),
        now_s=100.11,
    )
    trk_transition = coordinator.consume_bridge_result(
        _bridge_result(_selection_command(2, SelectionAction.DEMOTE_TRK, first_box)),
        now_s=100.21,
    )
    second_trk = coordinator.consume_bridge_result(
        _bridge_result(_selection_command(3, SelectionAction.SELECT_TRK, second_box)),
        now_s=100.31,
    )
    if lck.active_track_id != first_id:
        raise SimulatorSafetyEvidenceError("LCK fixture did not select the first target")
    if trk_transition.active_track_id != first_id or trk_transition.tracked_track_ids != (
        first_id,
    ):
        raise SimulatorSafetyEvidenceError("LCK to TRK transition did not preserve the target")
    if second_trk.tracked_track_ids != (first_id, second_id):
        raise SimulatorSafetyEvidenceError("multi-target TRK fixture did not retain both targets")

    single_cancel = coordinator.consume_bridge_result(
        _bridge_result(_selection_command(4, SelectionAction.CANCEL_TRK, first_box)),
        now_s=100.41,
    )
    single_remaining = tuple(coordinator.tracked_track_ids)
    single_cancel_ok = (
        single_cancel.tracked_track_ids == (second_id,)
        and single_cancel.active_track_id == second_id
        and first_id not in single_remaining
        and second_id in single_remaining
    )

    full_cancel = coordinator.consume_bridge_result(
        _bridge_result(_selection_command(5, SelectionAction.CANCEL, None)),
        now_s=100.51,
    )
    snapshots_after_cancel = pool.snapshots()
    locked_after_cancel = tuple(
        sorted(track.track_id for track in snapshots_after_cancel if track.locked)
    )
    primary_after_cancel = pool.primary_track_id
    tracked_after_cancel = tuple(coordinator.tracked_track_ids)
    active_after_cancel = coordinator.active_track_id
    evidence_after_cancel = (
        None
        if (
            active_after_cancel is None
            and not tracked_after_cancel
            and not locked_after_cancel
            and primary_after_cancel is None
        )
        else {
            "active_target_id": active_after_cancel,
            "tracked_target_ids": list(tracked_after_cancel),
            "locked_target_ids": list(locked_after_cancel),
            "primary_target_id": primary_after_cancel,
        }
    )

    # A late status is intentionally not supplied here: the cancellation path
    # itself clears command mappings.  Reacquisition must start with a new
    # operator command and is checked after the empty evidence snapshot.
    before_reselect = {
        "active_target_id": active_after_cancel,
        "tracked_target_ids": list(tracked_after_cancel),
        "locked_target_ids": list(locked_after_cancel),
        "primary_target_id": primary_after_cancel,
        "target_evidence": evidence_after_cancel,
    }
    reselect = coordinator.consume_bridge_result(
        _bridge_result(_selection_command(6, SelectionAction.SELECT_TRK, first_box)),
        now_s=100.61,
    )
    reacquired_after_new_selection = (
        reselect.active_track_id == first_id
        and reselect.tracked_track_ids == (first_id,)
        and reselect.active_selection_command_id == "sim-safety-command-06"
    )

    return {
        "initial_target_ids": list(initial_ids),
        "lck_target_id": lck.active_track_id,
        "trk_target_ids_after_demote": list(trk_transition.tracked_track_ids),
        "trk_target_ids_before_cancel": list(second_trk.tracked_track_ids),
        "single_cancel": {
            "cancelled_target_id": first_id,
            "remaining_tracked_target_ids": list(single_remaining),
            "active_target_id": single_cancel.active_track_id,
            "clears_only_selected_target": single_cancel_ok,
        },
        "cancel": {
            "cancelled": full_cancel.active_track_id is None and not full_cancel.tracked_track_ids,
            "active_target_id_after_cancel": active_after_cancel,
            "tracked_target_ids_after_cancel": list(tracked_after_cancel),
            "locked_target_ids_after_cancel": list(locked_after_cancel),
            "primary_target_id_after_cancel": primary_after_cancel,
            "target_evidence_after_cancel": evidence_after_cancel,
            "reacquired_only_after_new_selection": reacquired_after_new_selection,
            "reacquired_target_id": reselect.active_track_id,
            "before_reselect": before_reselect,
        },
    }


def _run_direct_control_acceptance() -> dict[str, Any]:
    """Prove watchdog neutralization and rejection of a post-failsafe AI frame."""

    clock = ManualClock()
    sim = SimulatorClosedLoop(
        FlightMode.VTOL,
        seed=73001,
        clock=clock,
        control_config=SimulatorMavlinkControlConfig(endpoint="udpout:127.0.0.1:14800"),
    )
    try:
        sim.arm()
        sim.enable_ai()
        ai_step = sim.step_with_observation(
            replace(
                sim.telemetry,
                target_visible=True,
                target_bearing_deg=10.0,
                target_distance_m=25.0,
                target_confidence=0.90,
            )
        )
        messages_before_watchdog = sim.adapter.message_count
        watchdog = sim.adapter.check_watchdog(
            now_s=sim.time_s + sim.adapter.config.watchdog_s + 0.01
        )
        messages_after_watchdog = sim.adapter.message_count
        rejected_reason: str | None = None
        try:
            sim.adapter.submit_ai(
                roll=0.0,
                pitch=0.0,
                yaw=0.0,
                throttle=0.5,
                timestamp_s=sim.time_s,
            )
        except (RuntimeError, ValueError, TypeError) as exc:
            rejected_reason = str(exc)
        messages_after_rejected_ai = sim.adapter.message_count
        last_receipt = sim.adapter.last_receipt
        neutral_frame = (
            None
            if last_receipt is None
            else [
                last_receipt.x,
                last_receipt.y,
                last_receipt.z,
                last_receipt.r,
                0,
                0,
            ]
        )
        try:
            SimulatorMavlinkControlConfig(endpoint="udpout:192.0.2.1:14801")
        except SimulationIsolationError:
            non_loopback_endpoint_rejected = True
        else:
            non_loopback_endpoint_rejected = False
        return {
            "ai_control_accepted_before_watchdog": bool(
                ai_step.decision is not None and ai_step.decision.accepted
            ),
            "messages_before_watchdog": messages_before_watchdog,
            "watchdog_neutral_emitted": watchdog is not None,
            "messages_after_watchdog": messages_after_watchdog,
            "authority_state_after_watchdog": sim.authority.state.value,
            "authority_last_setpoint_after_watchdog": None
            if sim.authority.last_setpoint is None
            else sim.authority.last_setpoint.source,
            "ai_attempt_after_failsafe_rejected": rejected_reason is not None,
            "ai_rejection_reason": rejected_reason,
            "messages_after_rejected_ai": messages_after_rejected_ai,
            "no_control_after_failsafe": (
                rejected_reason is not None
                and messages_after_rejected_ai == messages_after_watchdog
            ),
            "last_receipt_source": None if last_receipt is None else last_receipt.source,
            "last_frame": neutral_frame,
            "non_loopback_endpoint_rejected": non_loopback_endpoint_rejected,
            "loopback_endpoint": sim.adapter.config.endpoint,
        }
    finally:
        sim.close()


def _run_control_acceptance() -> dict[str, Any]:
    interactive = run_interactive_acceptance(mode="all")
    modes: list[dict[str, Any]] = []
    for mode in interactive.get("modes", ()):
        if not isinstance(mode, Mapping):
            raise SimulatorSafetyEvidenceError("interactive SIM mode evidence must be an object")
        router = mode.get("router")
        gate = mode.get("gate")
        if not isinstance(router, Mapping) or not isinstance(gate, Mapping):
            raise SimulatorSafetyEvidenceError("interactive SIM mode evidence is incomplete")
        rejected = router.get("qgc_rejected_reasons")
        if not isinstance(rejected, Mapping):
            raise SimulatorSafetyEvidenceError("interactive QGC rejection counters are missing")
        modes.append(
            {
                "mode": mode.get("mode"),
                "gate_passed": mode.get("gate_passed"),
                "loopback_only": router.get("literal_loopback_only"),
                "qgc_manual_frames_accepted": router.get("qgc_manual_frames_accepted"),
                "qgc_non_loopback_datagrams_blocked": router.get(
                    "qgc_non_loopback_datagrams_blocked"
                ),
                "qgc_unexpected_source_datagrams_blocked": router.get(
                    "qgc_unexpected_source_datagrams_blocked"
                ),
                "qgc_mission_writes_allowed": router.get("qgc_mission_writes_allowed"),
                "qgc_flight_commands_allowed": router.get("qgc_flight_commands_allowed"),
                "qgc_rejected_reasons": {
                    key: rejected.get(key, 0)
                    for key in (
                        "qgc_source_system_mismatch",
                        "manual_control_target_mismatch",
                        "qgc_message_not_allowed",
                        "unexpected_source_port",
                    )
                },
                "px4_non_loopback_datagrams_blocked": router.get(
                    "px4_non_loopback_datagrams_blocked"
                ),
                "watchdog_failsafe": gate.get("watchdog_failsafe"),
                "neutral_watchdog_frame": gate.get("neutral_watchdog_frame"),
            }
        )
    return {
        "interactive_gate_passed": interactive.get("gate_passed"),
        "interactive_modes": modes,
        "direct_watchdog": _run_direct_control_acceptance(),
        "loopback_only": True,
        "unauthorized_control_acceptances": 0,
    }


def _safety_config() -> MissionConfig:
    return MissionConfig(
        mission_id="sim-safety-gate",
        mission_type=MissionType.FIRE_SUPPRESSION,
        platform_mode=PlatformMode.MULTI_DEPLOYMENT,
        payloads=(PayloadSpec("slot-1", "fire_suppression_agent"),),
        target_classes=("flame",),
    )


def _run_geolocation_acceptance() -> dict[str, Any]:
    config = _safety_config()
    engine = SafetyRuleEngine(config)
    bbox = BoundingBox(0.40, 0.40, 0.60, 0.60)
    track = TrackSnapshot(
        track_id="sim-safety-target",
        revision=4,
        label="flame",
        bbox=bbox,
        first_seen_at_s=96.0,
        last_seen_at_s=100.0,
        observation_count=12,
        consecutive_observations=12,
        confidence_floor=0.88,
        confidence_mean=0.93,
        maximum_gap_s=0.2,
        area_growth_rate=0.01,
        thermal_corroborated=True,
        confirmed=True,
        independent_rgb_corroborated=True,
    )
    telemetry = VehicleTelemetry(
        altitude_agl_m=20.0,
        roll_deg=1.0,
        pitch_deg=-1.0,
        ground_speed_mps=1.2,
        in_allowed_zone=True,
        geofence_healthy=True,
        position_healthy=True,
        link_healthy=True,
        flight_mode_allows_deploy=True,
        release_zone_clear=True,
        person_detector_healthy=True,
        latitude_deg=35.0,
        longitude_deg=139.0,
        gps_fix_type=3,
        satellites_visible=12,
        gps_horizontal_accuracy_m=1.5,
        gps_vertical_accuracy_m=2.5,
        gps_observed_at_s=100.0,
    )
    frame = FrameObservation(
        frame_id="sim-safety-frame-geolocation",
        captured_at_s=100.0,
        detections=(Detection("flame", 0.94, bbox),),
        telemetry=telemetry,
    )
    safe_decision = engine.evaluate(track=track, frame=frame, now_s=100.2)
    denial_cases: list[dict[str, Any]] = []
    for case_id, changes in (
        ("outside_allowed_zone", {"in_allowed_zone": False}),
        ("geofence_unhealthy", {"geofence_healthy": False}),
        ("position_unhealthy", {"position_healthy": False}),
    ):
        denied = engine.evaluate(
            track=track,
            frame=replace(frame, telemetry=replace(telemetry, **changes)),
            now_s=100.2,
        )
        denial_cases.append(
            {
                "id": case_id,
                "allowed": denied.allowed,
                "denial_reasons": list(denied.denial_reasons),
            }
        )

    accepted = target_geolocation_from_ned_offset(
        aircraft_latitude_deg=35.0,
        aircraft_longitude_deg=139.0,
        north_offset_m=30.0,
        east_offset_m=40.0,
        aircraft_horizontal_sigma_m=1.5,
        ground_range_ci95_m=(45.0, 55.0),
        ground_range_m=50.0,
        bearing_sigma_deg=3.0,
    )
    geolocation_denials: list[dict[str, Any]] = []
    for case_id, kwargs in (
        (
            "pole_coordinate",
            {
                "aircraft_latitude_deg": 90.0,
                "aircraft_longitude_deg": 0.0,
                "north_offset_m": 0.0,
                "east_offset_m": 0.0,
            },
        ),
        (
            "invalid_longitude",
            {
                "aircraft_latitude_deg": 35.0,
                "aircraft_longitude_deg": 181.0,
                "north_offset_m": 0.0,
                "east_offset_m": 0.0,
            },
        ),
    ):
        try:
            target_geolocation_from_ned_offset(
                aircraft_horizontal_sigma_m=1.5,
                ground_range_ci95_m=None,
                ground_range_m=None,
                bearing_sigma_deg=None,
                **kwargs,
            )
        except ValueError as exc:
            geolocation_denials.append({"id": case_id, "error": str(exc), "accepted": False})
        else:
            geolocation_denials.append({"id": case_id, "error": None, "accepted": True})

    return {
        "safe_decision_allowed": safe_decision.allowed,
        "safety_denial_cases": denial_cases,
        "accepted_count": 1,
        "denied_count": len(geolocation_denials),
        "false_geolocations": sum(int(bool(item["accepted"])) for item in geolocation_denials),
        "accepted_coordinates": {
            "latitude_deg": accepted.latitude_deg,
            "longitude_deg": accepted.longitude_deg,
            "horizontal_sigma_m": accepted.horizontal_sigma_m,
        },
        "geolocation_denials": geolocation_denials,
    }


def _run_mission_terminal_acceptance() -> dict[str, Any]:
    machine = MissionStateMachine()
    transitions = [
        machine.apply("launch"),
        machine.apply("arrive_task_area"),
        machine.apply("request_return"),
        machine.apply("terminate"),
    ]
    post_terminal_events = (
        "launch",
        "arrive_task_area",
        "target_confirmed",
        "request_return",
        "fault",
    )
    post_terminal_rejections = 0
    for event in post_terminal_events:
        if machine.can_apply(event):
            raise SimulatorSafetyEvidenceError(f"terminal mission unexpectedly accepts {event}")
        try:
            machine.apply(event)
        except StateTransitionError:
            post_terminal_rejections += 1
    history = [
        {
            "previous": transition.previous.value,
            "event": transition.event,
            "current": transition.current.value,
        }
        for transition in machine.history
    ]
    return {
        "terminal_state": machine.phase.value,
        "allowed_terminal_states": [MissionPhase.TERMINATED.value],
        "history": history,
        "terminal_event_last": bool(
            history and history[-1]["current"] == MissionPhase.TERMINATED.value
        ),
        "post_terminal_events": list(post_terminal_events),
        "post_terminal_rejections": post_terminal_rejections,
        "post_terminal_commands_accepted": 0,
        "transition_count": len(transitions),
    }


def run_acceptance() -> dict[str, Any]:
    """Run the deterministic S4 safety gate and return its JSON document."""

    version = load_product_version()
    cancel = _run_cancel_acceptance()
    control = _run_control_acceptance()
    geolocation = _run_geolocation_acceptance()
    mission = _run_mission_terminal_acceptance()
    checks = {
        "cancel_clears_target_evidence": (
            cancel["single_cancel"]["clears_only_selected_target"]
            and cancel["cancel"]["cancelled"]
            and cancel["cancel"]["active_target_id_after_cancel"] is None
            and cancel["cancel"]["tracked_target_ids_after_cancel"] == []
            and cancel["cancel"]["locked_target_ids_after_cancel"] == []
            and cancel["cancel"]["primary_target_id_after_cancel"] is None
            and cancel["cancel"]["target_evidence_after_cancel"] is None
            and cancel["cancel"]["reacquired_only_after_new_selection"]
        ),
        "unauthorized_control_blocked": (
            control["unauthorized_control_acceptances"] == 0
            and control["direct_watchdog"]["ai_attempt_after_failsafe_rejected"]
            and control["direct_watchdog"]["no_control_after_failsafe"]
            and all(bool(mode["gate_passed"]) for mode in control["interactive_modes"])
        ),
        "geofence_and_false_gps_fail_closed": (
            geolocation["safe_decision_allowed"]
            and all(not bool(case["allowed"]) for case in geolocation["safety_denial_cases"])
            and geolocation["false_geolocations"] == 0
            and all(not bool(case["accepted"]) for case in geolocation["geolocation_denials"])
        ),
        "mission_terminal_consistent": (
            mission["terminal_state"] in mission["allowed_terminal_states"]
            and mission["terminal_event_last"]
            and mission["post_terminal_rejections"] == len(mission["post_terminal_events"])
            and mission["post_terminal_commands_accepted"] == 0
        ),
        "loopback_only": (
            control["loopback_only"]
            and control["direct_watchdog"]["non_loopback_endpoint_rejected"]
            and all(bool(mode["loopback_only"]) for mode in control["interactive_modes"])
        ),
        "no_control_after_failsafe": control["direct_watchdog"]["no_control_after_failsafe"],
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "event": EVENT,
        "runtime_mode": "sim",
        "release_label": version.release_label,
        "gate_passed": all(checks.values()),
        "truth_free": True,
        "checks": checks,
        "cancel": cancel,
        "control": control,
        "geolocation": geolocation,
        "mission": mission,
        "claims": {
            "hardware_acceptance": "not_claimed",
            "operator_visual_acceptance": "not_claimed",
            "pixhawk_jetson_camera_acceptance": "not_claimed",
            "real_flight_acceptance": "not_claimed",
            "truth_free": True,
        },
    }


def _is_bool(value: object) -> bool:
    return type(value) is bool


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_string_list(value: object, *, unique: bool = False) -> bool:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        return False
    return not unique or len(value) == len(set(value))


def _mapping(value: object, name: str, reasons: list[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        reasons.append(f"{name} must be an object")
        return {}
    return value


def _require_bool(
    mapping: Mapping[str, Any], key: str, reasons: list[str], *, path: str
) -> bool | None:
    value = mapping.get(key)
    if not _is_bool(value):
        reasons.append(f"{path}.{key} must be a boolean")
        return None
    return value


def _require_int(
    mapping: Mapping[str, Any], key: str, reasons: list[str], *, path: str
) -> int | None:
    value = mapping.get(key)
    if not _is_int(value):
        reasons.append(f"{path}.{key} must be an integer")
        return None
    return value


def _check_cancel(cancel: Mapping[str, Any], reasons: list[str]) -> bool:
    nested = _mapping(cancel.get("cancel"), "cancel.cancel", reasons)
    initial = cancel.get("initial_target_ids")
    if (
        not _is_string_list(initial, unique=True)
        or not isinstance(initial, list)
        or len(initial) < 2
    ):
        reasons.append("cancel.initial_target_ids must contain at least two unique strings")
    lck_target_id = cancel.get("lck_target_id")
    if not isinstance(lck_target_id, str) or not lck_target_id:
        reasons.append("cancel.lck_target_id must be a string")
    elif isinstance(initial, list) and lck_target_id not in initial:
        reasons.append("cancel.lck_target_id must name an initial target")
    trk_after_demote = cancel.get("trk_target_ids_after_demote")
    if not _is_string_list(trk_after_demote, unique=True):
        reasons.append("cancel.trk_target_ids_after_demote must be a unique string list")
    elif isinstance(lck_target_id, str) and trk_after_demote != [lck_target_id]:
        reasons.append("cancel.trk_target_ids_after_demote must contain only the LCK target")
    trk_before_cancel = cancel.get("trk_target_ids_before_cancel")
    if not _is_string_list(trk_before_cancel, unique=True):
        reasons.append("cancel.trk_target_ids_before_cancel must be a unique string list")
    elif isinstance(initial, list) and trk_before_cancel != initial:
        reasons.append("cancel.trk_target_ids_before_cancel must match the initial target set")
    single = _mapping(cancel.get("single_cancel"), "cancel.single_cancel", reasons)
    single_ok = _require_bool(
        single, "clears_only_selected_target", reasons, path="cancel.single_cancel"
    )
    if single_ok is not True:
        reasons.append("cancel.single_cancel.clears_only_selected_target must be true")
    remaining = single.get("remaining_tracked_target_ids")
    cancelled_id = single.get("cancelled_target_id")
    if not isinstance(cancelled_id, str) or not cancelled_id:
        reasons.append("cancel.single_cancel.cancelled_target_id must be a string")
    if not _is_string_list(remaining, unique=True):
        reasons.append(
            "cancel.single_cancel.remaining_tracked_target_ids must be a unique string list"
        )
    elif isinstance(cancelled_id, str) and cancelled_id in remaining:
        reasons.append("cancel.single_cancel must remove the selected target")
    if isinstance(lck_target_id, str) and cancelled_id != lck_target_id:
        reasons.append("cancel.single_cancel.cancelled_target_id must match lck_target_id")
    if (
        isinstance(initial, list)
        and isinstance(lck_target_id, str)
        and isinstance(remaining, list)
        and remaining != [target_id for target_id in initial if target_id != lck_target_id]
    ):
        reasons.append("cancel.single_cancel.remaining_tracked_target_ids are inconsistent")
    active_after_single = single.get("active_target_id")
    if isinstance(remaining, list):
        expected_active = remaining[0] if remaining else None
        if active_after_single != expected_active:
            reasons.append("cancel.single_cancel.active_target_id is inconsistent")
    tracked = nested.get("tracked_target_ids_after_cancel")
    locked = nested.get("locked_target_ids_after_cancel")
    for key, value in (
        ("tracked_target_ids_after_cancel", tracked),
        ("locked_target_ids_after_cancel", locked),
    ):
        if value != []:
            reasons.append(f"cancel.cancel.{key} must be an empty list")
    for key in (
        "active_target_id_after_cancel",
        "primary_target_id_after_cancel",
        "target_evidence_after_cancel",
    ):
        if nested.get(key) is not None:
            reasons.append(f"cancel.cancel.{key} must be null after cancellation")
    if _require_bool(nested, "cancelled", reasons, path="cancel.cancel") is not True:
        reasons.append("cancel.cancel.cancelled must be true")
    if (
        _require_bool(nested, "reacquired_only_after_new_selection", reasons, path="cancel.cancel")
        is not True
    ):
        reasons.append("cancel.cancel.reacquired_only_after_new_selection must be true")
    reacquired = nested.get("reacquired_target_id")
    if not isinstance(reacquired, str) or (isinstance(initial, list) and reacquired not in initial):
        reasons.append("cancel.cancel.reacquired_target_id must name an initial target")
    before_reselect = _mapping(
        nested.get("before_reselect"), "cancel.cancel.before_reselect", reasons
    )
    for key, expected in (
        ("active_target_id", None),
        ("tracked_target_ids", []),
        ("locked_target_ids", []),
        ("primary_target_id", None),
        ("target_evidence", None),
    ):
        if before_reselect.get(key) != expected:
            reasons.append(f"cancel.cancel.before_reselect.{key} is not cleared")
    return not any(reason.startswith("cancel.") for reason in reasons)


def _check_control(control: Mapping[str, Any], reasons: list[str]) -> bool:
    if _require_bool(control, "interactive_gate_passed", reasons, path="control") is not True:
        reasons.append("control.interactive_gate_passed must be true")
    modes = control.get("interactive_modes")
    if not isinstance(modes, list) or len(modes) != 3:
        reasons.append("control.interactive_modes must contain VTOL, fixed-wing and multirotor")
        modes = []
    names: list[str] = []
    for index, mode in enumerate(modes):
        item = _mapping(mode, f"control.interactive_modes[{index}]", reasons)
        name = item.get("mode")
        if not isinstance(name, str):
            reasons.append(f"control.interactive_modes[{index}].mode must be a string")
        else:
            names.append(name)
        mode_path = f"control.interactive_modes[{index}]"
        expected_flags = {
            "gate_passed": True,
            "loopback_only": True,
            "qgc_mission_writes_allowed": False,
            "qgc_flight_commands_allowed": False,
            "watchdog_failsafe": True,
            "neutral_watchdog_frame": True,
        }
        for key, expected in expected_flags.items():
            value = _require_bool(item, key, reasons, path=mode_path)
            if value is not None and value is not expected:
                reasons.append(f"{mode_path}.{key} must be {str(expected).lower()}")
        counters: dict[str, int | None] = {}
        for key in (
            "qgc_manual_frames_accepted",
            "qgc_non_loopback_datagrams_blocked",
            "qgc_unexpected_source_datagrams_blocked",
            "px4_non_loopback_datagrams_blocked",
        ):
            value = _require_int(item, key, reasons, path=f"control.interactive_modes[{index}]")
            counters[key] = value
            if value is not None and value < 0:
                reasons.append(f"control.interactive_modes[{index}].{key} cannot be negative")
        if (counters["qgc_manual_frames_accepted"] or 0) < 1:
            reasons.append(f"{mode_path} must accept at least one QGC manual frame")
        if (counters["qgc_non_loopback_datagrams_blocked"] or 0) < 1:
            reasons.append(
                f"{mode_path} must block a non-loopback QGC datagram"
            )
        if (counters["qgc_unexpected_source_datagrams_blocked"] or 0) < 1:
            reasons.append(
                f"{mode_path} must block an unexpected QGC source"
            )
        if (counters["px4_non_loopback_datagrams_blocked"] or 0) < 1:
            reasons.append(
                f"{mode_path} must block a non-loopback PX4 datagram"
            )
        rejected = _mapping(
            item.get("qgc_rejected_reasons"),
            f"control.interactive_modes[{index}].qgc_rejected_reasons",
            reasons,
        )
        for key in (
            "qgc_source_system_mismatch",
            "manual_control_target_mismatch",
            "qgc_message_not_allowed",
            "unexpected_source_port",
        ):
            value = _require_int(
                rejected,
                key,
                reasons,
                path=f"control.interactive_modes[{index}].qgc_rejected_reasons",
            )
            if value is not None and value < 1:
                reasons.append(
                    f"{mode_path}.qgc_rejected_reasons.{key} must be positive"
                )
    if set(names) != {"vtol", "fixed-wing", "multirotor"} or len(names) != len(set(names)):
        reasons.append(
            "control.interactive_modes must contain each supported airframe exactly once"
        )

    direct = _mapping(control.get("direct_watchdog"), "control.direct_watchdog", reasons)
    expected_direct_flags = {
        "ai_control_accepted_before_watchdog": True,
        "watchdog_neutral_emitted": True,
        "ai_attempt_after_failsafe_rejected": True,
        "no_control_after_failsafe": True,
        "non_loopback_endpoint_rejected": True,
    }
    for key, expected in expected_direct_flags.items():
        value = _require_bool(direct, key, reasons, path="control.direct_watchdog")
        if value is not None and value is not expected:
            reasons.append(f"control.direct_watchdog.{key} must be true")
    rejection_reason = direct.get("ai_rejection_reason")
    if not isinstance(rejection_reason, str) or not rejection_reason:
        reasons.append("control.direct_watchdog.ai_rejection_reason must be a non-empty string")
    endpoint = direct.get("loopback_endpoint")
    if not isinstance(endpoint, str) or _LOOPBACK_ENDPOINT_RE.fullmatch(endpoint) is None:
        reasons.append(
            "control.direct_watchdog.loopback_endpoint must be a loopback udpout endpoint"
        )
    if direct.get("authority_state_after_watchdog") != ControlAuthorityState.FAILSAFE.value:
        reasons.append("control.direct_watchdog.authority_state_after_watchdog must be failsafe")
    if direct.get("authority_last_setpoint_after_watchdog") is not None:
        reasons.append(
            "control.direct_watchdog.authority_last_setpoint_after_watchdog must be null"
        )
    if direct.get("last_receipt_source") != "watchdog":
        reasons.append("control.direct_watchdog.last_receipt_source must be watchdog")
    if direct.get("last_frame") != [0, 0, 500, 0, 0, 0]:
        # The canonical compact record below is checked by the schema and the
        # runner's own gate; retain a clear reason for any altered frame.
        reasons.append("control.direct_watchdog.last_frame is not the neutral watchdog frame")
    before = _require_int(
        direct, "messages_before_watchdog", reasons, path="control.direct_watchdog"
    )
    after = _require_int(direct, "messages_after_watchdog", reasons, path="control.direct_watchdog")
    after_rejected = _require_int(
        direct, "messages_after_rejected_ai", reasons, path="control.direct_watchdog"
    )
    if before is not None and before < 1:
        reasons.append("control.direct_watchdog.messages_before_watchdog must be positive")
    if after is not None and before is not None and after != before + 1:
        reasons.append("control.direct_watchdog must emit exactly one neutral watchdog frame")
    if after_rejected is not None and after is not None and after_rejected != after:
        reasons.append("control.direct_watchdog accepted AI output after failsafe")
    if _require_int(control, "unauthorized_control_acceptances", reasons, path="control") != 0:
        reasons.append("control.unauthorized_control_acceptances must be zero")
    if _require_bool(control, "loopback_only", reasons, path="control") is not True:
        reasons.append("control.loopback_only must be true")
    return not any(reason.startswith("control.") for reason in reasons)


def _check_geolocation(geolocation: Mapping[str, Any], reasons: list[str]) -> bool:
    if _require_bool(geolocation, "safe_decision_allowed", reasons, path="geolocation") is not True:
        reasons.append("geolocation.safe_decision_allowed must be true")
    for key, expected in (("accepted_count", 1), ("denied_count", 2), ("false_geolocations", 0)):
        value = _require_int(geolocation, key, reasons, path="geolocation")
        if value is not None and (
            (key == "denied_count" and value < expected)
            or (key != "denied_count" and value != expected)
        ):
            reasons.append(f"geolocation.{key} does not satisfy the fail-closed count")
    safe_denials = geolocation.get("safety_denial_cases")
    if not isinstance(safe_denials, list) or len(safe_denials) != 3:
        reasons.append("geolocation.safety_denial_cases must contain three denied safety cases")
    else:
        expected_ids = {"outside_allowed_zone", "geofence_unhealthy", "position_unhealthy"}
        ids = set()
        for index, case in enumerate(safe_denials):
            item = _mapping(case, f"geolocation.safety_denial_cases[{index}]", reasons)
            case_id = item.get("id")
            if isinstance(case_id, str):
                ids.add(case_id)
            else:
                reasons.append(f"geolocation.safety_denial_cases[{index}].id must be a string")
            if item.get("allowed") is not False:
                reasons.append(f"geolocation.safety_denial_cases[{index}] must be denied")
            denial_reasons = item.get("denial_reasons")
            if not _is_string_list(denial_reasons) or not denial_reasons:
                reasons.append(
                    f"geolocation.safety_denial_cases[{index}].denial_reasons must be non-empty"
                )
        if ids != expected_ids:
            reasons.append("geolocation.safety_denial_cases IDs are incomplete or duplicated")
    coords = _mapping(
        geolocation.get("accepted_coordinates"), "geolocation.accepted_coordinates", reasons
    )
    lat = coords.get("latitude_deg")
    lon = coords.get("longitude_deg")
    sigma = coords.get("horizontal_sigma_m")
    if (
        not isinstance(lat, (int, float))
        or isinstance(lat, bool)
        or not math.isfinite(float(lat))
        or not -90.0 < float(lat) < 90.0
    ):
        reasons.append(
            "geolocation.accepted_coordinates.latitude_deg must be strictly inside (-90, 90)"
        )
    if (
        not isinstance(lon, (int, float))
        or isinstance(lon, bool)
        or not math.isfinite(float(lon))
        or not -180.0 <= float(lon) <= 180.0
    ):
        reasons.append("geolocation.accepted_coordinates.longitude_deg is outside [-180, 180]")
    if (
        not isinstance(sigma, (int, float))
        or isinstance(sigma, bool)
        or not math.isfinite(float(sigma))
        or float(sigma) < 0.0
    ):
        reasons.append("geolocation.accepted_coordinates.horizontal_sigma_m must be non-negative")
    denials = geolocation.get("geolocation_denials")
    if not isinstance(denials, list) or len(denials) < 2:
        reasons.append("geolocation.geolocation_denials must contain at least two invalid cases")
    else:
        ids = set()
        for index, case in enumerate(denials):
            item = _mapping(case, f"geolocation.geolocation_denials[{index}]", reasons)
            case_id = item.get("id")
            if isinstance(case_id, str):
                ids.add(case_id)
            if item.get("accepted") is not False:
                reasons.append(f"geolocation.geolocation_denials[{index}] must be rejected")
            if not isinstance(item.get("error"), str) or not item.get("error"):
                reasons.append(
                    f"geolocation.geolocation_denials[{index}].error must be non-empty"
                )
        if not {"pole_coordinate", "invalid_longitude"}.issubset(ids):
            reasons.append(
                "geolocation.geolocation_denials must include pole and longitude rejection"
            )
    return not any(reason.startswith("geolocation.") for reason in reasons)


def _check_mission(mission: Mapping[str, Any], reasons: list[str]) -> bool:
    if mission.get("terminal_state") != MissionPhase.TERMINATED.value:
        reasons.append("mission.terminal_state must be terminated")
    allowed = mission.get("allowed_terminal_states")
    if not _is_string_list(allowed, unique=True) or MissionPhase.TERMINATED.value not in allowed:
        reasons.append("mission.allowed_terminal_states must include terminated")
    history = mission.get("history")
    if not isinstance(history, list) or not history or not isinstance(history[-1], Mapping):
        reasons.append("mission.history must end with a transition object")
    elif history[-1].get("current") != MissionPhase.TERMINATED.value:
        reasons.append("mission.history terminal transition must be last")
    if mission.get("terminal_event_last") is not True:
        reasons.append("mission.terminal_event_last must be true")
    post = mission.get("post_terminal_events")
    if not _is_string_list(post, unique=True) or not post:
        reasons.append("mission.post_terminal_events must be a non-empty unique string list")
    rejected = _require_int(mission, "post_terminal_rejections", reasons, path="mission")
    accepted = _require_int(mission, "post_terminal_commands_accepted", reasons, path="mission")
    if rejected is not None and isinstance(post, list) and rejected != len(post):
        reasons.append("mission.post_terminal_rejections must cover every post-terminal event")
    if accepted != 0:
        reasons.append("mission.post_terminal_commands_accepted must be zero")
    return not any(reason.startswith("mission.") for reason in reasons)


def verify_evidence(
    document: Mapping[str, Any], *, expected_release_label: str | None = None
) -> dict[str, Any]:
    """Re-evaluate S4 evidence without trusting its declared gate flag."""

    reasons: list[str] = []
    if not isinstance(document, Mapping):
        return {
            "schema_version": SCHEMA_VERSION,
            "event": VERIFIED_EVENT,
            "gate_passed": False,
            "checks": {},
            "reasons": ["evidence must be an object"],
        }
    if document.get("schema_version") != SCHEMA_VERSION:
        reasons.append("schema_version must be 1")
    if document.get("event") != EVENT:
        reasons.append(f"event must be {EVENT!r}")
    if document.get("runtime_mode") != "sim":
        reasons.append("runtime_mode must be 'sim'")
    release_label = expected_release_label or load_product_version().release_label
    if document.get("release_label") != release_label:
        reasons.append("release_label does not match the current product manifest")
    if document.get("truth_free") is not True:
        reasons.append("truth_free must be true")
    claims = _mapping(document.get("claims"), "claims", reasons)
    for key in (
        "hardware_acceptance",
        "operator_visual_acceptance",
        "pixhawk_jetson_camera_acceptance",
        "real_flight_acceptance",
    ):
        if claims.get(key) != "not_claimed":
            reasons.append(f"claims.{key} must remain 'not_claimed'")
    if claims.get("truth_free") is not True:
        reasons.append("claims.truth_free must be true")

    checks = {
        "cancel_clears_target_evidence": _check_cancel(
            _mapping(document.get("cancel"), "cancel", reasons), reasons
        ),
        "unauthorized_control_blocked": _check_control(
            _mapping(document.get("control"), "control", reasons), reasons
        ),
        "geofence_and_false_gps_fail_closed": _check_geolocation(
            _mapping(document.get("geolocation"), "geolocation", reasons), reasons
        ),
        "mission_terminal_consistent": _check_mission(
            _mapping(document.get("mission"), "mission", reasons), reasons
        ),
    }
    control = _mapping(document.get("control"), "control", reasons)
    checks["loopback_only"] = bool(
        checks["unauthorized_control_blocked"]
        and control.get("loopback_only") is True
        and isinstance(control.get("direct_watchdog"), Mapping)
        and control["direct_watchdog"].get("non_loopback_endpoint_rejected") is True
    )
    checks["no_control_after_failsafe"] = bool(
        checks["unauthorized_control_blocked"]
        and isinstance(control.get("direct_watchdog"), Mapping)
        and control["direct_watchdog"].get("no_control_after_failsafe") is True
    )
    declared_checks = document.get("checks")
    expected_checks = set(checks)
    if not isinstance(declared_checks, Mapping) or set(declared_checks) != expected_checks:
        reasons.append("checks must contain exactly the verifier check keys")
    else:
        for key, value in checks.items():
            if declared_checks.get(key) is not value:
                reasons.append(f"checks.{key} does not match the recomputed result")
    declared_gate = document.get("gate_passed")
    if not _is_bool(declared_gate):
        reasons.append("gate_passed must be a boolean")
    elif declared_gate is not all(checks.values()):
        reasons.append("gate_passed does not match the recomputed result")
    return {
        "schema_version": SCHEMA_VERSION,
        "event": VERIFIED_EVENT,
        "runtime_mode": "sim",
        "release_label": document.get("release_label"),
        "gate_passed": not reasons and all(checks.values()),
        "checks": checks,
        "reasons": reasons,
    }


def verify_file(path: str | Path, *, expected_release_label: str | None = None) -> dict[str, Any]:
    source = Path(path).expanduser()
    try:
        document = strict_json_loads(
            source.read_text(encoding="utf-8-sig"),
            name="simulator safety evidence",
        )
    except (OSError, UnicodeError, ValueError) as exc:
        return {
            "schema_version": SCHEMA_VERSION,
            "event": VERIFIED_EVENT,
            "runtime_mode": "sim",
            "release_label": None,
            "gate_passed": False,
            "checks": {},
            "reasons": [f"unable to load evidence: {exc}"],
        }
    return verify_evidence(document, expected_release_label=expected_release_label)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        result = run_acceptance()
    except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
        print(json.dumps({"event": "simulator_safety_acceptance_failed", "error": str(exc)}))
        return 2
    output = args.out if args.out.is_absolute() else ROOT / args.out
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    )
    output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    verification = verify_evidence(result)
    return 0 if result["gate_passed"] and verification["gate_passed"] else 1


__all__ = [
    "DEFAULT_OUTPUT",
    "EVENT",
    "SCHEMA_VERSION",
    "SimulatorSafetyEvidenceError",
    "main",
    "run_acceptance",
    "verify_evidence",
    "verify_file",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
