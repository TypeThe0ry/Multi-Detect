#!/usr/bin/env python3
"""Run the shared current-reference OnboardAI event path beside PX4/Gazebo.

This runner keeps the existing disposable Docker lifecycle and opens one
truth-free MAVLink telemetry stream.  Live PX4 heartbeat/GPS samples are used
to build ``ContextSnapshot`` values; structured detector/tracker observations
are then submitted to ``OnboardRealtimeModelBridge`` and the selected current
OnboardAI ONNX model.

By default, structured observations are deliberately synthetic and are only a
control-plane fixture for exercising the model/mission contract.  With
``--use-actual-detector`` and captured sensor frames, the candidate YOLO
detector and the shared RGB/color/depth adapter run on actual Gazebo frames.
Neither mode claims detector accuracy, vehicle motion, HIL, Jetson, REAL,
outdoor GNSS, or QGC acceptance.  The same adapter is intended for the future
Jetson camera process without changing the OnboardAI or Mission path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    ROOT = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(ROOT / "src"))
else:  # pragma: no cover - imported as a module
    ROOT = Path(__file__).resolve().parents[1]

from multidetect.autonomy import (  # noqa: E402
    MIN_TARGET_DETECTOR_CONFIDENCE,
    ContextSnapshot,
    FrameCameraPose,
    FramePerceptionResult,
    GeoPoint,
    ModelBackedDecisionProvider,
    OnboardAIExecutor,
    OnboardAutonomousSortie,
    OnboardFlightPlanDispatch,
    OnboardFramePerceptionAdapter,
    OnboardMissionAdapter,
    OnboardMissionEventLoop,
    OnboardOnnxModelRunner,
    OnboardPerceptionObservation,
    OnboardRealtimeModelBridge,
    OnboardTaskAction,
    PlanAction,
    PolicyArtifact,
    PromptEnvelope,
    ResolvedTaskSpec,
    SharedTaskPolicy,
    TaskConstraints,
    TaskGeometry,
    TaskType,
    load_onboard_model_reference,
)
from multidetect.autonomy.mission_adapter import build_external_px4_adapter  # noqa: E402
from multidetect.autonomy.onboard_navigation import compile_model_flight_plan  # noqa: E402
from multidetect.autonomy.serialization import canonical_sha256  # noqa: E402
from multidetect.autonomy.task_ranker import TaskRanker  # noqa: E402
from multidetect.autonomy.task_selection import TaskCandidate  # noqa: E402
from multidetect.mission_planning import parse_mission_compile_runtime  # noqa: E402
from multidetect.multimodal_ranging import (  # noqa: E402
    AircraftPose,
    CameraCalibration,
    load_camera_calibration,
)
from multidetect.perception_factory import COCO80_CLASS_NAMES  # noqa: E402
from multidetect.product_version import PRODUCT_VERSION  # noqa: E402
from multidetect.px4_mission import (  # noqa: E402
    MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
    PX4_AUTO_MISSION_CUSTOM_MODE,
    MultirotorMissionGeometry,
    Px4MultirotorSafetyPolicy,
)
from multidetect.simulator.stack import (
    airframe_profile_hash as stable_airframe_profile_hash,  # noqa: E402
)
from multidetect.vision import (  # noqa: E402
    OnnxRawYoloConfig,
    OnnxRawYoloDetector,
    TiledDetectionConfig,
    TiledDetectionFusion,
)

try:  # pytest imports ``scripts`` as a namespace package
    from scripts.gazebo_capture_plan import load_capture_plan  # noqa: E402
except ModuleNotFoundError:  # direct ``python scripts/...`` invocation
    from gazebo_capture_plan import load_capture_plan  # type: ignore[no-redef]

try:  # pytest imports ``scripts`` as a namespace package
    from scripts.build_gazebo_raw_recording_manifest import (  # noqa: E402
        build_recording as build_gazebo_raw_recording,
    )
except ModuleNotFoundError:  # direct ``python scripts/...`` invocation
    from build_gazebo_raw_recording_manifest import (  # type: ignore[no-redef]
        build_recording as build_gazebo_raw_recording,
    )

try:  # pytest imports ``scripts`` as a namespace package
    from scripts.run_simulator_docker import (  # noqa: E402
        PINNED_IMAGE,
        build_docker_simulator_plan,
    )
    from scripts.run_simulator_docker_acceptance import (  # noqa: E402
        DockerSimulatorAcceptanceError,
        DockerTelemetryProbe,
    )
    from scripts.run_simulator_docker_acceptance import (
        run_acceptance as run_docker_acceptance,
    )
    from scripts.run_simulator_docker_ai_acceptance import (
        ARM_MAX_ATTEMPTS,
        ARM_RETRY_INTERVAL_S,
        _prepare_disposable_sitl_for_arm,
        _resolve_docker_executable,
    )
except ModuleNotFoundError:  # direct ``python scripts/...`` invocation
    from run_simulator_docker import (  # type: ignore[no-redef]
        PINNED_IMAGE,
        build_docker_simulator_plan,
    )
    from run_simulator_docker_acceptance import (  # type: ignore[no-redef]
        DockerSimulatorAcceptanceError,
        DockerTelemetryProbe,
    )
    from run_simulator_docker_acceptance import (
        run_acceptance as run_docker_acceptance,
    )
    from run_simulator_docker_ai_acceptance import (  # type: ignore[no-redef]
        ARM_MAX_ATTEMPTS,
        ARM_RETRY_INTERVAL_S,
        _prepare_disposable_sitl_for_arm,
        _resolve_docker_executable,
    )


DEFAULT_SCENARIO = ROOT / "configs" / "scenarios" / "urban_inspection_multirotor_001.scenario.json"
DEFAULT_MODEL_REFERENCE = (
    ROOT / "configs" / "training" / "onboard-policy-current-reference-v1.json"
)
DEFAULT_MODEL = (
    ROOT
    / "artifacts"
    / "models"
    / "onboard-policy-r33-balanced-bearing-3d-dev"
    / "onboard_policy_r33.onnx"
)
DEFAULT_MODEL_ID = "onboard-policy-r33-balanced-bearing-3d-dev"
DEFAULT_MODEL_VERSION = (
    "0.3.0-pre.8-onboard-policy-r33-balanced-bearing-3d-dev"
)
DEFAULT_DETECTOR_MODEL = (
    ROOT / "artifacts" / "models" / "coco-yolo26n-traditional" / "yolo26n-traditional.onnx"
)
DEFAULT_CAMERA_CALIBRATION = (
    ROOT
    / "simulation"
    / "gazebo"
    / "calibration"
    / "gazebo-multirotor-front-camera-v1.json"
)
DEFAULT_EVIDENCE = (
    ROOT / "artifacts" / "evaluation" / "external-onboard-model-acceptance-20261004.json"
)
DEFAULT_CONTAINER_NAME = "multidetect-onboard-model"
DEFAULT_CONTROL_PORT = 24560
DEFAULT_TELEMETRY_PORT = 24561
PROFILE_ID = "px4_multirotor_gazebo_v1"
# Aerial targets occupy far fewer pixels than the detector's original
# ground-camera validation set.  Keep the low-confidence candidates truth-free
# and let the existing colour, depth, temporal, and OnboardAI gates decide;
# this is not a target acceptance threshold by itself.
ACTUAL_DETECTOR_CONFIDENCE_THRESHOLD = 0.01
MAX_OBSERVE_SECONDS = 3600.0
NOMINAL_FLIGHT_TIME_S = 1_800.0


class DisposableSitlAutonomousLifecycle:
    """Explicit arm/start/RTL seam for a disposable loopback SITL only.

    The model and Mission transaction still own task selection, planner
    revalidation, upload, readback, commit and permit.  This tiny adapter owns
    only the external vehicle lifecycle that a real Jetson/Pixhawk transport
    must provide.  It is never constructed by the default passive acceptance
    path; callers must opt in with ``--execute-autonomous-flight`` together
    with Mission transactions.
    """

    __slots__ = (
        "connection",
        "mavutil",
        "clock",
        "timeout_s",
        "started",
        "mode_confirmed",
        "armed_observed",
        "arm_ack_result",
        "return_requested",
        "home_arrival_confirmed",
        "report_committed",
        "mode_events",
        "command_events",
        "safety_mode_events",
        "safety_mode_confirmed",
    )

    def __init__(
        self,
        connection: object,
        *,
        mavutil_module: object | None = None,
        clock: Callable[[], float] = time.monotonic,
        timeout_s: float = 15.0,
    ) -> None:
        if connection is None or not hasattr(connection, "mav"):
            raise TypeError("autonomous lifecycle requires an injected MAVLink connection")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if not math.isfinite(float(timeout_s)) or float(timeout_s) <= 0.0:
            raise ValueError("timeout_s must be finite and positive")
        self.connection = connection
        self.mavutil = mavutil_module if mavutil_module is not None else _pymavutil()
        self.clock = clock
        self.timeout_s = float(timeout_s)
        self.started = False
        self.mode_confirmed = False
        self.armed_observed = False
        self.arm_ack_result: int | None = None
        self.return_requested = False
        self.home_arrival_confirmed = False
        self.report_committed = False
        self.mode_events: list[str] = []
        self.command_events: list[str] = []
        self.safety_mode_events: list[str] = []
        self.safety_mode_confirmed = False

    def _constants(self) -> object:
        module = self.mavutil
        constants = getattr(module, "mavlink", None) if module is not None else None
        if constants is None:
            raise DockerSimulatorAcceptanceError("pymavlink MAVLink constants are unavailable")
        return constants

    def _set_mode(self, mode: str) -> int:
        sender = getattr(getattr(self.connection, "mav", None), "set_mode_send", None)
        if mode == "AUTO.MISSION":
            if not callable(sender):
                raise DockerSimulatorAcceptanceError("MAVLink connection cannot set PX4 mode")
            sender(
                int(getattr(self.connection, "target_system", 1)),
                MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
                PX4_AUTO_MISSION_CUSTOM_MODE,
            )
            self.mode_events.append(mode)
            return PX4_AUTO_MISSION_CUSTOM_MODE
        mapping_getter = getattr(self.connection, "mode_mapping", None)
        if callable(mapping_getter) and callable(sender):
            mapping = mapping_getter() or {}
            custom_mode = mapping.get(mode)
            if custom_mode is not None:
                # pymavlink's PX4 map uses (base_mode, main_mode, sub_mode),
                # while other autopilots expose a packed integer.
                if isinstance(custom_mode, (tuple, list)) and len(custom_mode) == 3:
                    custom_mode = (int(custom_mode[1]) << 16) | (int(custom_mode[2]) << 24)
                sender(
                    int(getattr(self.connection, "target_system", 1)),
                    MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
                    int(custom_mode),
                )
                self.mode_events.append(mode)
                return int(custom_mode)
        raise DockerSimulatorAcceptanceError(
            f"PX4 mode {mode} has no verifiable custom-mode mapping; nothing was sent"
        )

    def _send_gcs_heartbeat(self) -> None:
        """Keep PX4's GCS liveness gate open on the same MAVLink socket."""

        sender = getattr(getattr(self.connection, "mav", None), "heartbeat_send", None)
        if not callable(sender):
            return
        constants = self._constants()
        try:
            sender(
                getattr(constants, "MAV_TYPE_GCS", 6),
                getattr(constants, "MAV_AUTOPILOT_INVALID", 8),
                0,
                0,
                0,
            )
            self.command_events.append("GCS_HEARTBEAT")
        except Exception:
            # A minimal test double may not implement a full heartbeat wire
            # method.  The live path still receives and records PX4 ACKs.
            return

    @staticmethod
    def _message_type(message: object) -> str:
        getter = getattr(message, "get_type", None)
        if callable(getter):
            return str(getter())
        if isinstance(message, Mapping):
            return str(message.get("type", ""))
        return str(getattr(message, "_type", ""))

    @staticmethod
    def _field(message: object, name: str, default: object = None) -> object:
        if isinstance(message, Mapping):
            return message.get(name, default)
        return getattr(message, name, default)

    def _wait_armed(self, *, timeout_s: float | None = None) -> bool:
        receiver = getattr(self.connection, "recv_match", None)
        if not callable(receiver):
            raise DockerSimulatorAcceptanceError(
                "MAVLink connection cannot receive arm confirmation"
            )
        wait_timeout = self.timeout_s if timeout_s is None else float(timeout_s)
        deadline = self.clock() + wait_timeout
        constants = self._constants()
        armed_flag = int(getattr(constants, "MAV_MODE_FLAG_SAFETY_ARMED", 128))
        command_id = int(getattr(constants, "MAV_CMD_COMPONENT_ARM_DISARM", 400))
        accepted = int(getattr(constants, "MAV_RESULT_ACCEPTED", 0))
        while self.clock() < deadline:
            remaining = max(0.01, min(0.25, deadline - self.clock()))
            try:
                message = receiver(blocking=True, timeout=remaining)
            except TypeError:
                message = receiver(blocking=True, timeout=remaining)
            if message is None:
                continue
            kind = self._message_type(message)
            if kind == "COMMAND_ACK":
                try:
                    ack_command = int(self._field(message, "command", -1))
                    ack_result = int(self._field(message, "result", -1))
                except (TypeError, ValueError):
                    ack_command = ack_result = -1
                if ack_command == command_id:
                    self.arm_ack_result = ack_result
                    if ack_result != accepted:
                        # PX4 commonly reports TEMPORARILY_REJECTED while the
                        # estimator/commander finishes settling.  The existing
                        # Docker AI acceptance retries this boundedly; do the
                        # same here instead of turning the first transient ACK
                        # into a false terminal failure.
                        temporarily_rejected = int(
                            getattr(constants, "MAV_RESULT_TEMPORARILY_REJECTED", 1)
                        )
                        in_progress = int(getattr(constants, "MAV_RESULT_IN_PROGRESS", 5))
                        if ack_result in {temporarily_rejected, in_progress}:
                            continue
                        raise DockerSimulatorAcceptanceError(
                            f"PX4 rejected ARM command with MAV_RESULT={ack_result}"
                        )
                continue
            if kind != "HEARTBEAT":
                continue
            try:
                base_mode = int(self._field(message, "base_mode", 0))
            except (TypeError, ValueError):
                base_mode = 0
            if base_mode & armed_flag:
                self.armed_observed = True
                return True
        return False

    def _wait_mode(self, expected_custom_mode: int, *, mode: str) -> None:
        receiver = getattr(self.connection, "recv_match", None)
        if not callable(receiver):
            raise DockerSimulatorAcceptanceError(
                "MAVLink connection cannot receive mode confirmation"
            )
        deadline = self.clock() + self.timeout_s
        while self.clock() < deadline:
            remaining = max(0.01, min(0.25, deadline - self.clock()))
            try:
                message = receiver(type="HEARTBEAT", blocking=True, timeout=remaining)
            except TypeError:
                message = receiver(blocking=True, timeout=remaining)
            if message is None or self._message_type(message) != "HEARTBEAT":
                continue
            try:
                custom_mode = int(self._field(message, "custom_mode", -1))
                base_mode = int(self._field(message, "base_mode", 0))
            except (TypeError, ValueError):
                continue
            if (
                custom_mode == int(expected_custom_mode)
                and base_mode & MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
            ):
                self.mode_confirmed = True
                return
        raise DockerSimulatorAcceptanceError(f"PX4 did not confirm {mode} on the heartbeat")

    def _arm(self) -> None:
        sender = getattr(getattr(self.connection, "mav", None), "command_long_send", None)
        if not callable(sender):
            raise DockerSimulatorAcceptanceError("MAVLink connection cannot send ARM command")
        constants = self._constants()
        # PX4 rejects command sources that have not advertised a live GCS.
        # Send this immediately before ARM on the same loopback transport,
        # matching the existing Docker AI acceptance path.
        deadline = self.clock() + self.timeout_s
        for attempt in range(1, ARM_MAX_ATTEMPTS + 1):
            if self.clock() >= deadline:
                break
            self._send_gcs_heartbeat()
            sender(
                int(getattr(self.connection, "target_system", 1)),
                int(getattr(self.connection, "target_component", 1)),
                int(getattr(constants, "MAV_CMD_COMPONENT_ARM_DISARM", 400)),
                0,
                1.0,
                21196.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
            )
            self.command_events.append(
                "MAV_CMD_COMPONENT_ARM_DISARM:arm"
                if attempt == 1
                else f"MAV_CMD_COMPONENT_ARM_DISARM:arm_retry_{attempt}"
            )
            wait_s = min(ARM_RETRY_INTERVAL_S, max(0.01, deadline - self.clock()))
            if self._wait_armed(timeout_s=wait_s):
                return
        raise DockerSimulatorAcceptanceError(
            "PX4 arm command was sent but an armed heartbeat was not observed"
            f" after {ARM_MAX_ATTEMPTS} bounded attempts"
        )

    def start(self, _context: ContextSnapshot, _mission: object, _revision: object) -> None:
        """Start or re-enter AUTO.MISSION after a permitted Mission upload.

        The initial Mission arms once.  A model-generated replacement must
        re-confirm AUTO.MISSION so PX4 begins the newly uploaded route at its
        takeoff/search item, but it must never issue a second ARM command.
        """

        if self.started:
            if bool(getattr(_revision, "mission_replaced", False)):
                expected_mode = self._set_mode("AUTO.MISSION")
                self._wait_mode(expected_mode, mode="AUTO.MISSION")
            return
        expected_mode = self._set_mode("AUTO.MISSION")
        self._wait_mode(expected_mode, mode="AUTO.MISSION")
        self._arm()
        self.started = True

    def request_return(self, _context: ContextSnapshot, _revision: object) -> None:
        if self.return_requested:
            return
        if not self.started or not self.armed_observed:
            raise DockerSimulatorAcceptanceError("RTL requires a confirmed autonomous start")
        expected_mode = self._set_mode("RTL")
        self._wait_mode(expected_mode, mode="RTL")
        self.command_events.append("PX4_MODE:RTL")
        self.return_requested = True

    def dispatch_safety(self, decision: object) -> None:
        """Map a safety decision to an ACKed PX4 mode on the same socket.

        The normal model-requested return uses the same idempotent RTL seam
        as the sortie.  Every response must be mode-confirmed before the
        adapter clears its logical Mission observer.
        """

        response = getattr(getattr(decision, "response", None), "value", None)
        model_proposal = getattr(decision, "model_proposal", None)
        task_action = getattr(getattr(model_proposal, "task_action", None), "value", None)
        flight_plan = getattr(model_proposal, "flight_plan", None)
        flight_action = getattr(getattr(flight_plan, "action", None), "value", None)
        if response == "hold" and task_action == "report":
            # REPORT is record-only after a telemetry-proven return, not an
            # airborne emergency HOLD.  Preserve RTL and its landing lifecycle.
            if not self.return_requested or not self.home_arrival_confirmed:
                raise DockerSimulatorAcceptanceError(
                    "REPORT requires a telemetry-confirmed return to Home"
                )
            marker = "REPORT:record_only_no_mode_change"
            if marker not in self.safety_mode_events:
                self.safety_mode_events.append(marker)
            return
        if (
            response == "return"
            and task_action in {"record_location", "return_home"}
            and flight_action == "return_home"
        ):
            self.request_return(None, None)
            marker = "RETURN:RTL"
            if marker not in self.safety_mode_events:
                self.safety_mode_events.append(marker)
            self.safety_mode_confirmed = True
            return
        mode_by_response = {"return": "RTL", "hold": "POSCTL", "land": "LAND"}
        mode = mode_by_response.get(response)
        if mode is None:
            raise DockerSimulatorAcceptanceError(
                f"unsupported safety response for PX4 mode dispatch: {response!r}"
            )
        marker = f"{response.upper()}:{mode}"
        if marker in self.safety_mode_events:
            return
        expected_mode = self._set_mode(mode)
        self._wait_mode(expected_mode, mode=mode)
        self.safety_mode_events.append(marker)
        self.safety_mode_confirmed = True

    def report(self, _receipt: object) -> None:
        self.report_committed = True

    def evidence(self) -> dict[str, object]:
        return {
            "enabled": True,
            "scope": "disposable_loopback_sitl_only",
            "started": self.started,
            "mode_confirmed": self.mode_confirmed,
            "armed_observed": self.armed_observed,
            "arm_ack_result": self.arm_ack_result,
            "return_requested": self.return_requested,
            "home_arrival_confirmed": self.home_arrival_confirmed,
            "report_committed": self.report_committed,
            "mode_events": list(self.mode_events),
            "command_events": list(self.command_events),
            "safety_mode_events": list(self.safety_mode_events),
            "safety_mode_confirmed": self.safety_mode_confirmed,
            "physical_output_enabled": False,
            "jetson_hardware_evidence": False,
            "hil_real_evidence": False,
        }


def _pymavutil() -> object | None:
    try:
        from pymavlink import mavutil
    except ImportError:
        return None
    return mavutil


def _build_actual_perception_adapter(detector_model: Path) -> OnboardFramePerceptionAdapter:
    """Build the same candidate RGB detector boundary used by the Jetson path.

    The model is intentionally still quarantined for evidence generation.  The
    adapter is nevertheless the production-shaped boundary: RGB pixels enter
    the detector, color/depth are measured from that frame, and only the
    truth-free observation enters the shared OnboardAI bridge.
    """

    if not detector_model.is_file():
        raise DockerSimulatorAcceptanceError(
            f"RGB detector model file does not exist: {detector_model}"
        )
    calibration = load_camera_calibration(DEFAULT_CAMERA_CALIBRATION)
    detector = OnnxRawYoloDetector(
        OnnxRawYoloConfig(
            model_path=detector_model,
            class_names=COCO80_CLASS_NAMES,
            input_width=640,
            input_height=640,
            confidence_threshold=ACTUAL_DETECTOR_CONFIDENCE_THRESHOLD,
            providers=("CPUExecutionProvider",),
            model_version="ultralytics-yolo26n-coco80-v56-quarantined-candidate",
        )
    )
    # The vehicle-camera target is small after the full 1280x960 frame is
    # letterboxed into the detector's 640x640 input.  Reuse the exact same
    # YOLO weights in a bounded 2x2 tile pass so small distant targets get
    # native pixels without lowering the actionable .25 confidence gate.
    tiled_detector = TiledDetectionFusion(
        detector,
        TiledDetectionConfig(
            columns=2,
            rows=2,
            overlap_fraction=0.20,
            scan_interval_frames=1,
            tile_confidence_threshold=0.25,
            tile_confidence_by_label={"car": 0.25, "vehicle": 0.25},
            tile_labels=frozenset({"car", "vehicle"}),
        ),
    )
    return OnboardFramePerceptionAdapter(
        tiled_detector,
        # The repository-owned 3-D target is neutral.  Colour is not a
        # detector identity; it becomes a constraint only when the prompt
        # explicitly requests one.  The default external campaign therefore
        # trains/evaluates class + track + metric depth, not a red-car fixture.
        target_class="vehicle",
        target_color=None,
        fx_px=calibration.fx_px,
        cx_px=calibration.cx_px,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _detector_model_path(detector: object) -> Path | None:
    """Resolve the underlying model through optional tiled-detector wrapping."""

    current = detector
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        config = getattr(current, "config", None)
        model_path = getattr(config, "model_path", None)
        if isinstance(model_path, Path):
            return model_path
        current = getattr(current, "detector", None)
    return None


def _task(home: GeoPoint) -> ResolvedTaskSpec:
    return ResolvedTaskSpec(
        task_id="neutral-target-external-search",
        task_type=TaskType.CORRIDOR_VISUAL_SEARCH,
        geometry=TaskGeometry(
            kind="map_feature",
            resolved_feature_id="external-search-corridor",
            direction="northbound",
            length_m=400.0,
            centerline=(home, GeoPoint(home.latitude_deg + 0.003, home.longitude_deg)),
            resolution_confidence=0.95,
            buffer_m=20.0,
        ),
        target_classes=("vehicle",),
        on_detection=(PlanAction.OBSERVE_TARGET, PlanAction.CAPTURE_EVIDENCE),
        constraints=TaskConstraints(40.0, 900.0, True),
        completion_action=PlanAction.RETURN,
    )


def _runtime(
    home: GeoPoint,
    *,
    capture_route: tuple[Mapping[str, object], ...] | None = None,
) -> dict[str, object]:
    if capture_route is None:
        route = (
            (22.0, 11.0, 11.0),
            (44.0, 22.0, 12.0),
        )
    else:
        route = tuple(
            (
                float(item["north_m"]),
                float(item["east_m"]),
                float(item["altitude_m"]),
            )
            for item in capture_route
        )
        if len(route) < 2:
            raise ValueError("capture route requires at least two waypoints")

    def to_geo(north_m: float, east_m: float, altitude_m: float) -> GeoPoint:
        latitude = home.latitude_deg + north_m / 111_111.0
        longitude_scale = max(math.cos(math.radians(home.latitude_deg)), 0.1)
        longitude = home.longitude_deg + east_m / (111_111.0 * longitude_scale)
        return GeoPoint(latitude, longitude, altitude_m, "relative_home")

    geometry = MultirotorMissionGeometry(
        home=home,
        waypoints=tuple(to_geo(north, east, altitude) for north, east, altitude in route),
        # Keep the disposable Gazebo campaign above the takeoff gate while
        # avoiding a multi-minute climb before the model can search laterally.
        takeoff_altitude_m=11.0,
        loiter_time_s=15.0,
        loiter_radius_m=25.0,
        acceptance_radius_m=5.0,
        landing=GeoPoint(home.latitude_deg, home.longitude_deg, 0.0, "relative_home"),
    )
    policy = Px4MultirotorSafetyPolicy(
        geofence_center=home,
        geofence_radius_m=2_000.0,
        allowed_airframe_profile_ids=(PROFILE_ID,),
        minimum_relative_altitude_m=10.0,
        # The external neutral-target search campaign is deliberately bounded to
        # the 12 m search envelope used by its Gazebo camera geometry.  The
        # model may propose a generic altitude up to 120 m, but the canonical
        # navigation compiler projects that proposal into this reviewed
        # scenario policy before a PX4 Mission is uploaded.
        maximum_relative_altitude_m=12.0,
        maximum_mission_radius_m=1_000.0,
        maximum_loiter_radius_m=100.0,
        maximum_landing_offset_m=10.0,
    )
    return {
        "schema_version": 1,
        "vehicle_class": "multirotor",
        **geometry.to_mapping(),
        "safety_policy": policy.to_mapping(),
    }


def _context(
    probe: DockerTelemetryProbe,
    *,
    captured_at_s: float,
    airframe_profile_hash: str,
    home: GeoPoint,
) -> ContextSnapshot | None:
    position = probe.position
    gps = probe.gps
    if not isinstance(position, Mapping) or not isinstance(gps, Mapping):
        return None
    try:
        latitude = float(position["latitude_deg"])
        longitude = float(position["longitude_deg"])
        altitude = float(position.get("relative_altitude_m", 0.0))
        fix_type = int(gps.get("fix_type", 0))
        eph_cm = int(gps.get("eph_cm", 0))
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    accuracy = eph_cm / 100.0 if eph_cm > 0 else 1.0
    battery_remaining_pct = getattr(probe, "battery_remaining_pct", None)
    ground_speed_mps = getattr(probe, "ground_speed_mps", None)
    airspeed_mps = getattr(probe, "airspeed_mps", None)
    nominal_flight_time_s = getattr(probe, "nominal_flight_time_s", None)
    if not all(
        isinstance(value, (int, float)) and math.isfinite(float(value))
        for value in (
            battery_remaining_pct,
            ground_speed_mps,
            airspeed_mps,
            nominal_flight_time_s,
        )
    ):
        return None
    battery_remaining_pct = float(battery_remaining_pct)
    ground_speed_mps = float(ground_speed_mps)
    airspeed_mps = float(airspeed_mps)
    nominal_flight_time_s = float(nominal_flight_time_s)
    if not (0.0 <= battery_remaining_pct <= 100.0):
        return None
    if ground_speed_mps < 0.0 or airspeed_mps < 0.0 or nominal_flight_time_s <= 0.0:
        return None
    # MAVLink exposes remaining percentage, not a remaining-endurance estimate.
    # Scale the configured airframe baseline explicitly and retain the source in
    # evidence; never pretend that a fixed 1800 s value came from PX4 telemetry.
    estimated_flight_time_s = nominal_flight_time_s * battery_remaining_pct / 100.0
    if not (
        math.isfinite(latitude)
        and math.isfinite(longitude)
        and math.isfinite(altitude)
        and -90.0 < latitude < 90.0
        and -180.0 <= longitude <= 180.0
        and fix_type >= 3
    ):
        return None
    return ContextSnapshot(
        snapshot_id=f"external-px4-context-{captured_at_s:.6f}",
        captured_at_s=captured_at_s,
        captured_at_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        aircraft_position=GeoPoint(latitude, longitude, altitude, "relative_home"),
        home_position=home,
        airframe_profile_id=PROFILE_ID,
        airframe_profile_hash=airframe_profile_hash,
        battery_remaining_pct=battery_remaining_pct,
        estimated_flight_time_s=estimated_flight_time_s,
        ground_speed_mps=ground_speed_mps,
        airspeed_mps=airspeed_mps,
        wind_north_mps=0.0,
        wind_east_mps=0.0,
        gps_fix_type=fix_type,
        gps_horizontal_accuracy_m=accuracy,
        geofence_valid=bool(getattr(probe, "geofence_valid_declared", False)),
        camera_available=bool(getattr(probe, "camera_available_declared", False)),
        perception_available=bool(getattr(probe, "perception_available_declared", False)),
        map_cache_available=bool(getattr(probe, "map_cache_available_declared", False)),
        dem_available=bool(getattr(probe, "dem_available_declared", False)),
    )


def _frame_context(
    probe: DockerTelemetryProbe,
    *,
    provenance: object,
    airframe_profile_hash: str,
    home: GeoPoint,
) -> ContextSnapshot | None:
    """Bind the decision context to the same pose/GPS sample as the RGB frame.

    The PX4 stream still supplies health/energy fields, but the frame's
    Gazebo wire-v3 telemetry owns the aircraft latitude/longitude and GPS
    quality used for target projection.  This prevents a delayed RGB frame
    from being paired with a newer vehicle position and heading.
    """

    captured_at_s = getattr(provenance, "captured_at_s", None)
    frame_telemetry = getattr(provenance, "frame_telemetry", None)
    if not isinstance(captured_at_s, (int, float)) or not math.isfinite(float(captured_at_s)):
        return None
    if frame_telemetry is None:
        return None
    base = _context(
        probe,
        captured_at_s=float(captured_at_s),
        airframe_profile_hash=airframe_profile_hash,
        home=home,
    )
    if base is None:
        return None
    try:
        latitude = float(frame_telemetry.latitude_deg)
        longitude = float(frame_telemetry.longitude_deg)
        fix_type = int(frame_telemetry.gps_fix_type)
        accuracy = float(frame_telemetry.gps_horizontal_accuracy_m)
    except (TypeError, ValueError, AttributeError, OverflowError):
        return None
    if (
        not math.isfinite(latitude)
        or not math.isfinite(longitude)
        or not math.isfinite(accuracy)
        or not -90.0 < latitude < 90.0
        or not -180.0 <= longitude <= 180.0
        or fix_type < 3
        or accuracy < 0.0
    ):
        return None
    return replace(
        base,
        aircraft_position=GeoPoint(
            latitude,
            longitude,
            base.aircraft_position.altitude_m,
            "relative_home",
        ),
        gps_fix_type=fix_type,
        gps_horizontal_accuracy_m=accuracy,
    )


def _frame_camera_pose(
    provenance: object,
    *,
    source_frame_sha256: str,
    width: int,
    height: int,
    calibration: CameraCalibration,
) -> FrameCameraPose | None:
    """Build a full roll/pitch/heading camera pose from one sensor frame."""

    captured_at_s = getattr(provenance, "captured_at_s", None)
    telemetry = getattr(provenance, "frame_telemetry", None)
    if telemetry is None or not isinstance(captured_at_s, (int, float)):
        return None
    if (calibration.width_px, calibration.height_px) != (width, height):
        # A mismatched calibration must fail closed rather than silently fall
        # back to the old heading-only polar projection.
        return None
    try:
        pose = AircraftPose(
            captured_at_s=float(captured_at_s),
            roll_deg=float(telemetry.roll_deg),
            pitch_deg=float(telemetry.pitch_deg),
            heading_deg=float(telemetry.heading_deg) % 360.0,
            roll_sigma_deg=float(telemetry.attitude_roll_sigma_deg),
            pitch_sigma_deg=float(telemetry.attitude_pitch_sigma_deg),
            heading_sigma_deg=float(telemetry.attitude_heading_sigma_deg),
            uncertainty_source=str(telemetry.attitude_uncertainty_source),
        )
    except (TypeError, ValueError, AttributeError, OverflowError):
        return None
    return FrameCameraPose(
        calibration=calibration,
        aircraft_pose=pose,
        source_frame_sha256=source_frame_sha256,
    )


def _airframe_profile_hash(plan: object) -> str:
    document = {
        "schema_version": 1,
        "airframe_profile_id": str(plan.airframe_profile_id),
        "vehicle_model": str(plan.vehicle_model),
        "model_name": str(plan.model_name),
        "stack_plan_sha256": str(plan.stack_plan_sha256),
    }
    return stable_airframe_profile_hash(document)


def _build_bridge(
    *,
    model_path: Path,
    model_sha256: str,
    airframe_hash: str,
    context: ContextSnapshot,
    installed: list[object],
    safety_requests: list[object],
    model_id: str = DEFAULT_MODEL_ID,
    model_version: str = DEFAULT_MODEL_VERSION,
    mission_installer: Callable[[object | None], None] | None = None,
    safety_dispatcher: Callable[[object], None] | None = None,
    mission_transaction_dispatcher: Callable[[ContextSnapshot, object | None, object | None], None]
    | None = None,
    mission_runtime: Mapping[str, object] | None = None,
) -> OnboardRealtimeModelBridge:
    home = context.home_position
    task = TaskCandidate(_task(home))
    candidates = (task,)
    runtimes = {
        task.task.task_id: (
            dict(mission_runtime) if mission_runtime is not None else _runtime(home)
        )
    }
    ranker = TaskRanker((0, 0, 0), "external-onboard-current-reference")
    artifact = PolicyArtifact.for_ranker(
        ranker,
        policy_id="onboard-task-policy",
        policy_version=PRODUCT_VERSION.release_label,
        airframe_profile_hash=airframe_hash,
        mission_contract_hash=hashlib.sha256(b"multidetect.mission-compile-runtime-v1").hexdigest(),
        safety_policy_hash=hashlib.sha256(b"multidetect.safety-policy-v1").hexdigest(),
    )
    prompt = PromptEnvelope(
        prompt_id="external-neutral-target-prompt",
        text="搜索任务目标，确认后记录位置并返航",
        language="zh-CN",
        issued_at="2026-10-04T00:00:00Z",
        operator_id="external-software-sim",
        aircraft_id="gazebo-x500",
    )
    runner = OnboardOnnxModelRunner.from_path(model_path, providers=("CPUExecutionProvider",))
    provider = ModelBackedDecisionProvider(
        runner,
        model_id=model_id,
        model_version=model_version,
        model_sha256=model_sha256,
        provider="onnxruntime:CPUExecutionProvider",
    )
    executor = OnboardAIExecutor(
        prompt,
        SharedTaskPolicy(ranker, artifact),
        "multirotor",
        airframe_hash,
        decision_provider=provider,
    )

    def dispatch(current_context, flight_plan, decision, mission_id):
        compilation = compile_model_flight_plan(
            task.task,
            current_context,
            "multirotor",
            runtime=runtimes[task.task.task_id],
            flight_plan=flight_plan,
            airframe_profile_hash=airframe_hash,
            mission_id=mission_id,
        )
        return OnboardFlightPlanDispatch(
            mission=compilation.compiled_mission.executable_mission,
            flight_plan_sha256=flight_plan.canonical_hash,
            mission_sha256=compilation.executable_mission_hash,
            planner_revalidated=True,
            plan_sha256=compilation.plan.canonical_hash,
            wire_hash=compilation.wire_hash,
            compilation=compilation,
            selected_task_id=decision.selected_task_id,
        )

    loop = OnboardMissionEventLoop(
        OnboardMissionAdapter(
            executor,
            mission_id_prefix="external-onboard",
            mission_installer=installed.append if mission_installer is None else mission_installer,
            mission_transaction_dispatcher=mission_transaction_dispatcher,
            safety_dispatcher=(
                safety_requests.append if safety_dispatcher is None else safety_dispatcher
            ),
            flight_plan_dispatcher=dispatch,
        )
    )
    return OnboardRealtimeModelBridge(loop, candidates=candidates, runtimes=runtimes)


class ExternalOnboardDockerProbe(DockerTelemetryProbe):
    """Receive live PX4 telemetry and submit observations to the current model."""

    def __init__(
        self,
        endpoint: str,
        *,
        model_path: Path,
        model_sha256: str,
        airframe_hash: str,
        model_id: str = DEFAULT_MODEL_ID,
        model_version: str = DEFAULT_MODEL_VERSION,
        connection_factory: Callable[..., Any] | None = None,
        mavutil_module: object | None = None,
        clock: Callable[[], float] = time.monotonic,
        sensor_frames: list[tuple[object, object, object | None]] | None = None,
        frame_perception_adapter: OnboardFramePerceptionAdapter | None = None,
        execute_mission_transactions: bool = False,
        mission_journal_directory: Path | None = None,
        execute_autonomous_flight: bool = False,
        autonomous_flight_timeout_s: float = 15.0,
        sensor_capture_start_event: threading.Event | None = None,
        capture_route: tuple[Mapping[str, object], ...] | None = None,
    ) -> None:
        super().__init__(
            endpoint,
            connection_factory=connection_factory,
            mavutil_module=mavutil_module,
            clock=clock,
        )
        self.model_path = model_path
        self.model_sha256 = model_sha256
        self.model_id = model_id
        self.model_version = model_version
        self.airframe_hash = airframe_hash
        self.home: GeoPoint | None = None
        self.bridge: OnboardRealtimeModelBridge | None = None
        self.installed_missions: list[object] = []
        self.safety_requests: list[object] = []
        self.safety_dispatch_errors: list[str] = []
        self.mission_clear_events: list[Mapping[str, object]] = []
        self.logical_mission_clear_count = 0
        self.physical_mission_clear_sent = False
        self.onboard_error: str | None = None
        self.observation_index = 0
        self.observe_result: dict[str, object] | None = None
        self.sensor_frames = sensor_frames if sensor_frames is not None else []
        self.frame_perception_adapter = frame_perception_adapter
        self.camera_calibration: CameraCalibration | None = None
        self.execute_mission_transactions = bool(execute_mission_transactions)
        self.execute_autonomous_flight = bool(execute_autonomous_flight)
        self.autonomous_flight_timeout_s = float(autonomous_flight_timeout_s)
        self.sensor_capture_start_event = sensor_capture_start_event
        self.capture_route = capture_route
        self.sortie: OnboardAutonomousSortie | None = None
        self.autonomous_lifecycle: DisposableSitlAutonomousLifecycle | None = None
        self.arm_preflight: Mapping[str, object] | None = None
        self.return_completion_observed = False
        self.report_completion_observed = False
        self.mission_journal_directory = (
            mission_journal_directory.resolve()
            if mission_journal_directory is not None
            else ROOT / "artifacts" / "evaluation"
        )
        self.mission_transactions: list[Mapping[str, object]] = []
        self.perception_results: list[FramePerceptionResult] = []
        # Truth-free pose samples bound to each consumed RGB frame.  These are
        # sensor provenance for diagnosing a slow/rotating SITL search path;
        # they are never passed to the detector or used as evaluator truth.
        self.frame_telemetry_samples: list[dict[str, object]] = []
        self.takeoff_completion_observed = False
        self.takeoff_telemetry_at_s: float | None = None
        self.stale_sensor_frames_rejected = 0
        self.battery_remaining_pct: float | None = None
        self.ground_speed_mps: float | None = None
        self.airspeed_mps: float | None = None
        self.heading_deg: float | None = None
        self.heading_source: str | None = None
        self.ground_speed_source: str | None = None
        self.airspeed_source: str | None = None
        self.nominal_flight_time_s = NOMINAL_FLIGHT_TIME_S
        # These are explicit declarations for this control-plane fixture.  They
        # are not claims that a Gazebo camera/depth transport was observed.
        self.geofence_valid_declared = True
        self.camera_available_declared = True
        self.perception_available_declared = True
        self.map_cache_available_declared = True
        self.dem_available_declared = True

    def _install_mission(self, mission: object | None) -> None:
        """Keep logical Mission clearing separate from physical Mission uploads."""

        if mission is None:
            self.mission_clear_events.append(
                {
                    "kind": "logical_observer_clear",
                    "physical_clear_sent": False,
                }
            )
            return
        self.installed_missions.append(mission)

    def _dispatch_safety(self, decision: object) -> None:
        self.safety_requests.append(decision)
        if self.autonomous_lifecycle is not None:
            try:
                self.autonomous_lifecycle.dispatch_safety(decision)
            except Exception as exc:
                self.safety_dispatch_errors.append(str(exc))
                raise

    def _record_logical_mission_clear(self, context: ContextSnapshot) -> None:
        lifecycle = self.autonomous_lifecycle
        if lifecycle is None or not lifecycle.safety_mode_confirmed:
            raise DockerSimulatorAcceptanceError(
                "external logical Mission clear requires a confirmed PX4 safety mode"
            )
        self.logical_mission_clear_count += 1
        self.mission_clear_events.append(
            {
                "kind": "logical_safety_mission_clear",
                "physical_clear_sent": False,
                "context_sha256": context.canonical_hash,
            }
        )

    @staticmethod
    def _message_field(message: object, name: str, default: object = None) -> object:
        if isinstance(message, Mapping):
            return message.get(name, default)
        return getattr(message, name, default)

    def _record(self, message: object) -> None:
        super()._record(message)
        getter = getattr(message, "get_type", None)
        if callable(getter):
            kind = str(getter())
        elif isinstance(message, Mapping):
            kind = str(message.get("type", ""))
        else:
            kind = str(getattr(message, "_type", ""))
        if kind == "SYS_STATUS":
            try:
                battery = float(self._message_field(message, "battery_remaining"))
            except (TypeError, ValueError):
                battery = -1.0
            if math.isfinite(battery) and 0.0 <= battery <= 100.0:
                self.battery_remaining_pct = battery
        elif kind == "VFR_HUD":
            try:
                ground = float(self._message_field(message, "groundspeed"))
                air = float(self._message_field(message, "airspeed"))
            except (TypeError, ValueError):
                return
            try:
                heading = float(self._message_field(message, "heading"))
            except (TypeError, ValueError):
                heading = math.nan
            if math.isfinite(ground) and ground >= 0.0:
                self.ground_speed_mps = ground
                self.ground_speed_source = "PX4_VFR_HUD.groundspeed"
            if math.isfinite(air) and air >= 0.0:
                self.airspeed_mps = air
                self.airspeed_source = "PX4_VFR_HUD.airspeed"
            if math.isfinite(heading) and 0.0 <= heading < 360.0:
                self.heading_deg = heading
                self.heading_source = "PX4_VFR_HUD.heading"
        elif kind == "GLOBAL_POSITION_INT":
            try:
                velocity_north = float(self._message_field(message, "vx")) / 100.0
                velocity_east = float(self._message_field(message, "vy")) / 100.0
            except (TypeError, ValueError):
                velocity_north = velocity_east = 0.0
            try:
                heading = float(self._message_field(message, "hdg")) / 100.0
            except (TypeError, ValueError):
                heading = math.nan
            if math.isfinite(heading) and 0.0 <= heading < 360.0:
                self.heading_deg = heading
                self.heading_source = "PX4_GLOBAL_POSITION_INT.hdg"
            ground = math.hypot(velocity_north, velocity_east)
            if math.isfinite(ground) and ground >= 0.0:
                if self.ground_speed_mps is None:
                    self.ground_speed_mps = ground
                    self.ground_speed_source = "PX4_GLOBAL_POSITION_INT.vx_vy"
                # Multirotors in this scenario have no pitot/airspeed sensor.
                # Keep the ContextSnapshot complete using an explicit proxy;
                # this is never reported as measured airspeed.
                if self.airspeed_mps is None:
                    self.airspeed_mps = ground
                    self.airspeed_source = "multirotor_groundspeed_proxy_no_pitot"
            if (
                self.sensor_capture_start_event is not None
                and self.execute_autonomous_flight
                and self.autonomous_lifecycle is not None
                and self.autonomous_lifecycle.started
                and self.position is not None
                and float(self.position.get("relative_altitude_m", 0.0)) >= 10.0
            ):
                # The camera worker must not open on the preflight batch.  Open
                # it only after the same live telemetry stream has observed an
                # armed vehicle above the takeoff barrier.
                self.sensor_capture_start_event.set()

    def _submit_actual_frame(self, now: float) -> None:
        if self.bridge is None or self.home is None or self.frame_perception_adapter is None:
            return
        if self.observation_index >= len(self.sensor_frames):
            return
        frame, provenance, depth = self.sensor_frames[self.observation_index]
        image = getattr(frame, "image_bgr", None)
        if image is None:
            raise DockerSimulatorAcceptanceError(
                "Gazebo sensor frame did not expose an RGB image for the detector"
            )
        frame_id = str(getattr(provenance, "frame_id", ""))
        source_hash = str(getattr(provenance, "payload_sha256", ""))
        if not frame_id or len(source_hash) != 64:
            raise DockerSimulatorAcceptanceError(
                "Gazebo sensor provenance is incomplete for detector binding"
            )
        live_context = _context(
            self,
            captured_at_s=now,
            airframe_profile_hash=self.airframe_hash,
            home=self.home,
        )
        if live_context is None:
            return
        if self.bridge.event_loop.adapter.task_state.phase is OnboardTaskAction.TAKEOFF:
            altitude_m = live_context.aircraft_position.altitude_m
            if altitude_m is None or altitude_m < 10.0:
                # Sensor transport and PX4 telemetry are independent streams.
                # Keep the frame at the current index and wait for qualified
                # live telemetry instead of consuming it or feeding a frame
                # into the model before the shared lifecycle gate is closed.
                return
            if self.sortie is not None:
                self.sortie.ingest_telemetry(live_context, minimum_relative_altitude_m=10.0)
            else:
                self.bridge.mark_takeoff_complete(
                    live_context,
                    minimum_relative_altitude_m=10.0,
                )
            self.takeoff_completion_observed = True
            self.takeoff_telemetry_at_s = now
        captured_at_s = getattr(provenance, "captured_at_s", None)
        if (
            self.execute_autonomous_flight
            and self.takeoff_telemetry_at_s is not None
            and isinstance(captured_at_s, (int, float))
            and float(captured_at_s) < self.takeoff_telemetry_at_s
        ):
            # The base Docker runner may have captured a finite frame batch
            # before the lifecycle reached altitude.  Do not replay those
            # pixels as if they were flight-time camera observations.
            self.stale_sensor_frames_rejected += 1
            self.observation_index += 1
            return
        frame_context = _frame_context(
            self,
            provenance=provenance,
            airframe_profile_hash=self.airframe_hash,
            home=self.home,
        )
        if frame_context is None:
            # The frame must carry its own synchronized GPS/pose provenance;
            # never fall back to a latest-telemetry location for target GPS.
            self.stale_sensor_frames_rejected += 1
            self.observation_index += 1
            return
        frame_telemetry = getattr(provenance, "frame_telemetry", None)
        if frame_telemetry is not None:
            self.frame_telemetry_samples.append(
                {
                    "frame_id": frame_id,
                    "captured_at_s": float(captured_at_s),
                    "latitude_deg": float(frame_telemetry.latitude_deg),
                    "longitude_deg": float(frame_telemetry.longitude_deg),
                    "altitude_agl_m": float(frame_telemetry.altitude_agl_m),
                    "local_east_m": float(frame_telemetry.local_east_m),
                    "local_north_m": float(frame_telemetry.local_north_m),
                    "heading_deg": float(frame_telemetry.heading_deg),
                    "velocity_east_mps": float(frame_telemetry.velocity_east_mps),
                    "velocity_north_mps": float(frame_telemetry.velocity_north_mps),
                    "source": "gazebo_wire_v3_frame_telemetry",
                }
            )
        if self.camera_calibration is None:
            self.camera_calibration = load_camera_calibration(DEFAULT_CAMERA_CALIBRATION)
        frame_time = float(captured_at_s)
        frame_pose = _frame_camera_pose(
            provenance,
            source_frame_sha256=source_hash,
            width=int(getattr(frame, "width", 0)),
            height=int(getattr(frame, "height", 0)),
            calibration=self.camera_calibration,
        )
        result = self.frame_perception_adapter.observe(
            observation_id=f"gazebo-rgb-{frame_id}",
            # The decision event and the full camera pose use the original
            # sensor-frame timestamp.  Pairing both with ``frame_context``
            # prevents delayed RGB/depth data from being treated as current.
            observed_at_s=frame_time,
            image_bgr=image,
            source_frame_sha256=source_hash,
            depth_frame=depth,
            aircraft_heading_deg=self.heading_deg,
            camera_pose=frame_pose,
        )
        observation = result.observation
        if self.sortie is not None:
            # Keep the camera adapter's temporal confirmation in charge.  A
            # single low-confidence RGB/depth frame may enter SEARCH, but it
            # must never be promoted directly to MATCH_TARGET ->
            # RECORD_LOCATION -> RTL.  The next causal frame must carry the
            # same-track IoU confirmation before the model can propose the
            # recording phase.  This is the same path used by the Jetson
            # adapter; no external-SIM shortcut is allowed here.
            self.sortie.ingest_perception(frame_context, observation)
        else:
            self.bridge.submit(frame_context, result.observation)
        if observation is not result.observation:
            result = replace(result, observation=observation)
        self.perception_results.append(result)
        self.observation_index += 1

    def _submit_live_fixture(self, now: float) -> None:
        if self.bridge is None or self.home is None:
            return
        current = _context(
            self,
            captured_at_s=now,
            airframe_profile_hash=self.airframe_hash,
            home=self.home,
        )
        if current is None:
            return
        if self.sortie is not None and self.sortie.status.phase is OnboardTaskAction.TAKEOFF:
            altitude_m = current.aircraft_position.altitude_m
            if altitude_m is None or altitude_m < 10.0:
                # Structured observations must cross the exact same live
                # altitude barrier as RGB/depth frames.  This prevents the
                # synthetic control-plane fixture from being more permissive
                # than the future Jetson camera transport.
                return
            self.sortie.ingest_telemetry(current, minimum_relative_altitude_m=10.0)
            self.takeoff_completion_observed = True
        if self.observation_index == 0:
            observation = OnboardPerceptionObservation(
                observation_id="external-nonmatching-candidate",
                observed_at_s=now,
                source="synthetic-structured-perception-fixture",
                target_visible=True,
                target_class="candidate_object",
                target_color=None,
                target_class_match=False,
                target_color_match=False,
                target_bearing_deg=-80.0,
                target_distance_m=80.0,
                target_confidence=0.91,
                target_match_score=0.12,
                obstacle_distance_m=5_000.0,
                task_phase=OnboardTaskAction.SEARCH,
                detail="candidate object does not match the task class; model searches again",
            )
        elif self.observation_index == 1:
            observation = OnboardPerceptionObservation(
                observation_id="external-target-match",
                observed_at_s=now,
                source="synthetic-structured-perception-fixture",
                target_visible=True,
                target_class="vehicle",
                target_color=None,
                target_class_match=True,
                target_color_match=None,
                target_bearing_deg=15.0,
                target_distance_m=55.0,
                target_confidence=0.97,
                target_match_score=0.98,
                obstacle_distance_m=5_000.0,
                task_phase=OnboardTaskAction.RECORD_LOCATION,
                detail="task target matched; model follows the confirmed track",
            )
        else:
            observation = OnboardPerceptionObservation(
                observation_id="external-target-location",
                observed_at_s=now,
                source="synthetic-structured-perception-fixture",
                target_visible=True,
                target_class="vehicle",
                target_color=None,
                target_class_match=True,
                target_color_match=None,
                target_bearing_deg=15.0,
                target_distance_m=55.0,
                target_north_offset_m=52.0,
                target_east_offset_m=14.0,
                target_confidence=0.99,
                target_match_score=0.99,
                obstacle_distance_m=5_000.0,
                task_phase=OnboardTaskAction.MATCH_TARGET,
                detail="task target location confirmed with GPS offset",
            )
        if self.sortie is not None:
            if self.observation_index >= 2:
                self.sortie.record_target_location(current, observation)
            else:
                self.sortie.ingest_perception(current, observation)
        else:
            self.bridge.submit(current, observation)
        self.observation_index += 1

    def _maybe_complete_return(self, current: ContextSnapshot) -> None:
        """Close RTL only after live telemetry proves Home arrival."""

        sortie = self.sortie
        if sortie is None or self.return_completion_observed:
            return
        if not sortie.status.return_requested or not sortie.status.takeoff_confirmed:
            return
        position = current.aircraft_position
        home = current.home_position
        if position.altitude_m is None or position.altitude_m > 3.0:
            return
        # WGS-84 local approximation is sufficient for the bounded Home gate;
        # target geolocation still comes from the shared perception adapter.
        north_m = (position.latitude_deg - home.latitude_deg) * 111_320.0
        east_m = (
            (position.longitude_deg - home.longitude_deg)
            * 111_320.0
            * math.cos(math.radians(home.latitude_deg))
        )
        if math.hypot(north_m, east_m) > 15.0:
            return
        if self.autonomous_lifecycle is not None:
            self.autonomous_lifecycle.home_arrival_confirmed = True
        sortie.complete_return(current)
        self.return_completion_observed = True
        sortie.report_after_return(current)
        self.report_completion_observed = sortie.status.report_committed

    def observe(self, timeout_s: float) -> dict[str, object]:
        if not math.isfinite(timeout_s) or timeout_s <= 0.0:
            raise DockerSimulatorAcceptanceError("telemetry timeout must be finite and positive")
        connection: Any | None = None
        started = self.clock()
        deadline = started + min(float(timeout_s), MAX_OBSERVE_SECONDS)
        error: str | None = None
        try:
            connection = self._connect()
            next_heartbeat = started
            while self.clock() < deadline:
                now = self.clock()
                if now >= next_heartbeat:
                    self._send_heartbeat(connection)
                    next_heartbeat = now + 0.5
                receiver = getattr(connection, "recv_match", None)
                if not callable(receiver):
                    raise DockerSimulatorAcceptanceError("MAVLink connection has no recv_match")
                remaining = max(0.01, min(0.25, deadline - now))
                try:
                    message = receiver(blocking=True, timeout=remaining)
                except TypeError:
                    message = receiver(timeout=remaining)
                if message is not None:
                    self._record(message)
                if self.heartbeat is None or self.gps is None:
                    continue
                if int(self.gps.get("fix_type", 0)) < 3:
                    continue
                if int(self.gps.get("lat", 0)) == 0 or int(self.gps.get("lon", 0)) == 0:
                    continue
                if self.position is None:
                    continue
                if self.home is None:
                    self.home = GeoPoint(
                        float(self.position["latitude_deg"]),
                        float(self.position["longitude_deg"]),
                        0.0,
                        "relative_home",
                    )
                if self.bridge is None:
                    context = _context(
                        self,
                        captured_at_s=now,
                        airframe_profile_hash=self.airframe_hash,
                        home=self.home,
                    )
                    if context is None:
                        continue
                    mission_transaction_dispatcher = None
                    if self.execute_mission_transactions:
                        _geometry, mission_policy = parse_mission_compile_runtime(
                            _runtime(self.home, capture_route=self.capture_route), "multirotor"
                        )
                        external_adapter = build_external_px4_adapter(
                            "multirotor",
                            connection,
                            airframe_profile_id=PROFILE_ID,
                        )

                        def dispatch_external_transaction(
                            transaction_context: ContextSnapshot,
                            _mission: object | None,
                            compilation: object | None,
                            *,
                            _external_adapter=external_adapter,
                            _mission_policy=mission_policy,
                        ) -> None:
                            if _mission is None or compilation is None:
                                # Safety mode has already been ACKed by the
                                # lifecycle dispatcher.  PX4 Mission clear-all
                                # is intentionally forbidden; this callback
                                # records only the logical observer clear.
                                if _mission is not None or compilation is not None:
                                    raise DockerSimulatorAcceptanceError(
                                        "external Mission transport received a partial clear"
                                    )
                                self._record_logical_mission_clear(transaction_context)
                                return
                            transaction_result = _external_adapter.execute(
                                compilation,
                                context=transaction_context,
                                policy=_mission_policy,
                                journal_path=(
                                    self.mission_journal_directory
                                    / f"external-onboard-{len(self.mission_transactions):04d}.jsonl"
                                ),
                                now_utc=datetime.now(timezone.utc),
                                transaction_id=(
                                    f"external-onboard-{len(self.mission_transactions):04d}"
                                ),
                            )
                            self.mission_transactions.append(dict(transaction_result))

                        mission_transaction_dispatcher = dispatch_external_transaction
                    if self.execute_autonomous_flight:
                        if not self.execute_mission_transactions:
                            raise DockerSimulatorAcceptanceError(
                                "autonomous flight requires execute_mission_transactions"
                            )
                        self.autonomous_lifecycle = DisposableSitlAutonomousLifecycle(
                            connection,
                            timeout_s=self.autonomous_flight_timeout_s,
                        )
                    self.bridge = _build_bridge(
                        model_path=self.model_path,
                        model_sha256=self.model_sha256,
                        model_id=self.model_id,
                        model_version=self.model_version,
                        airframe_hash=self.airframe_hash,
                        context=context,
                        installed=self.installed_missions,
                        safety_requests=self.safety_requests,
                        mission_installer=self._install_mission,
                        safety_dispatcher=self._dispatch_safety,
                        mission_transaction_dispatcher=mission_transaction_dispatcher,
                        mission_runtime=_runtime(
                            context.home_position,
                            capture_route=self.capture_route,
                        ),
                    )
                    if self.execute_autonomous_flight:
                        self.sortie = OnboardAutonomousSortie(
                            self.bridge,
                            mission_start_dispatcher=self.autonomous_lifecycle.start,
                            return_request_dispatcher=self.autonomous_lifecycle.request_return,
                            report_dispatcher=self.autonomous_lifecycle.report,
                        )
                        self.sortie.prepare(
                            context,
                            mission_id="external-onboard-initial",
                        )
                    else:
                        self.bridge.prepare(context, mission_id="external-onboard-initial")
                # Once the shared model has recorded the target and requested
                # RTL, subsequent camera pixels belong to a new observation
                # phase and must not reopen MATCH_TARGET/SEARCH while the
                # vehicle is returning.  Keep consuming live PX4 telemetry
                # until the home/report gates close.
                if self.frame_perception_adapter is not None:
                    if not (self.sortie is not None and self.sortie.status.return_requested):
                        self._submit_actual_frame(now)
                elif self.observation_index < (3 if self.sortie is not None else 2):
                    self._submit_live_fixture(now)
                if self.sortie is not None:
                    current = _context(
                        self,
                        captured_at_s=now,
                        airframe_profile_hash=self.airframe_hash,
                        home=self.home,
                    )
                    if current is not None:
                        self._maybe_complete_return(current)
                if self.sortie is None and self.observation_index >= 2:
                    break
                if self.sortie is not None and self.report_completion_observed:
                    break
        except (
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            DockerSimulatorAcceptanceError,
        ) as exc:
            error = str(exc)
            self.onboard_error = error
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
        result: dict[str, object] = {
            "status": (
                "passed"
                if self.bridge is not None
                and self.observation_index >= 2
                and (not self.execute_autonomous_flight or self.report_completion_observed)
                else "failed"
            ),
            "endpoint": self.endpoint,
            "heartbeat_observed": self.heartbeat is not None,
            "gps_observed": self.gps is not None,
            "qualified": self.bridge is not None,
            "message_count": len(self.messages),
            "truth_free": True,
            "onboard_model": {
                "model_path": str(self.model_path),
                "model_sha256": self.model_sha256,
                "model_id": self.model_id,
                "model_version": self.model_version,
                "airframe_profile_sha256": self.airframe_hash,
                "bridge": None if self.bridge is None else self.bridge.evidence(),
                "installed_mission_count": len(self.installed_missions),
                "mission_transaction_count": len(self.mission_transactions),
                "mission_transport_enabled": self.execute_mission_transactions,
                "safety_request_count": len(self.safety_requests),
                "safety_requests": [
                    item.to_mapping() if hasattr(item, "to_mapping") else str(item)
                    for item in self.safety_requests
                ],
                "safety_dispatch_errors": list(self.safety_dispatch_errors),
                "logical_mission_clear_count": self.logical_mission_clear_count,
                "mission_clear_events": list(self.mission_clear_events),
                "physical_mission_clear_sent": self.physical_mission_clear_sent,
                "synthetic_structured_perception": self.frame_perception_adapter is None,
                "actual_rgb_detector_used": self.frame_perception_adapter is not None,
                "takeoff_completion_observed": self.takeoff_completion_observed,
                "takeoff_telemetry_at_s": self.takeoff_telemetry_at_s,
                "stale_sensor_frames_rejected": self.stale_sensor_frames_rejected,
                "autonomous_flight_enabled": self.execute_autonomous_flight,
                "arm_preflight": (None if self.arm_preflight is None else dict(self.arm_preflight)),
                "autonomous_lifecycle": (
                    None
                    if self.autonomous_lifecycle is None
                    else self.autonomous_lifecycle.evidence()
                ),
                "autonomous_sortie": (
                    None if self.sortie is None else self.sortie.status.to_mapping()
                ),
                "return_completion_observed": self.return_completion_observed,
                "report_completion_observed": self.report_completion_observed,
                "autonomous_flight_completion_gate": (
                    "passed"
                    if not self.execute_autonomous_flight or self.report_completion_observed
                    else "waiting_for_live_rtl_home_and_report"
                ),
                "actual_detector_model_sha256": (
                    _sha256(model_path)
                    if self.frame_perception_adapter is not None
                    and (model_path := _detector_model_path(self.frame_perception_adapter.detector))
                    is not None
                    else None
                ),
                "actual_detector_confidence_threshold": (
                    ACTUAL_DETECTOR_CONFIDENCE_THRESHOLD
                    if self.frame_perception_adapter is not None
                    else None
                ),
                "actionable_detector_confidence_threshold": (
                    MIN_TARGET_DETECTOR_CONFIDENCE
                    if self.frame_perception_adapter is not None
                    else None
                ),
                "actual_perception_results": [
                    result.to_mapping() for result in self.perception_results
                ],
                "frame_telemetry_samples": list(self.frame_telemetry_samples),
                "visual_accuracy": "not_claimed",
                "policy_applied_to_actuator": False,
                "context_sources": {
                    "battery_remaining_pct": "PX4_SYS_STATUS.battery_remaining",
                    "ground_speed_mps": self.ground_speed_source,
                    "airspeed_mps": self.airspeed_source,
                    "estimated_flight_time_s": (
                        "configured_nominal_flight_time_scaled_by_live_battery"
                    ),
                    "wind": "not_observed_assumed_zero_for_control_plane_only",
                    "camera_and_perception": (
                        "gazebo_rgb_detector_adapter"
                        if self.frame_perception_adapter is not None
                        else "declared_structured_perception_fixture_not_gazebo_frames"
                    ),
                    "geofence_map_dem": "declared_from_test_plan_not_runtime_proof",
                },
                "live_telemetry_context": {
                    "battery_remaining_pct": self.battery_remaining_pct,
                    "ground_speed_mps": self.ground_speed_mps,
                    "airspeed_mps": self.airspeed_mps,
                    "heading_deg": self.heading_deg,
                    "heading_source": self.heading_source,
                    "nominal_flight_time_s": self.nominal_flight_time_s,
                },
                "gazebo_sensor_frames": [
                    {
                        "frame_id": str(getattr(provenance, "frame_id", "")),
                        "sequence": int(getattr(provenance, "sequence", -1)),
                        "captured_at_s": float(getattr(provenance, "captured_at_s", 0.0)),
                        "rgb_payload_sha256": str(getattr(provenance, "payload_sha256", "")),
                        "depth_payload_sha256": (
                            str(getattr(depth, "payload_sha256", "")) if depth is not None else None
                        ),
                        "source": "gazebo_wire_v3_actual_sensor_frame",
                    }
                    for _frame, provenance, depth in self.sensor_frames
                ],
            },
            "hil_real_evidence": False,
            "jetson_hardware_evidence": False,
            "outdoor_gnss_evidence": False,
            "final_release_allowed": False,
        }
        if error:
            result["error"] = error
        self.observe_result = result
        return result


def _scenario_seed_from_runtime(
    scenario_path: Path,
    scenario_document: Mapping[str, object],
) -> int:
    """Resolve the seed through the scenario's hash-bound runtime reference."""

    direct_seed = scenario_document.get("seed")
    if isinstance(direct_seed, int) and not isinstance(direct_seed, bool) and direct_seed >= 0:
        return direct_seed
    channels = scenario_document.get("channels")
    if not isinstance(channels, Mapping):
        raise DockerSimulatorAcceptanceError("scenario has no runtime_input channel")
    runtime_reference = channels.get("runtime_input")
    if not isinstance(runtime_reference, Mapping):
        raise DockerSimulatorAcceptanceError("scenario runtime_input reference is invalid")
    runtime_name = runtime_reference.get("path")
    runtime_sha = runtime_reference.get("sha256")
    if not isinstance(runtime_name, str) or Path(runtime_name).name != runtime_name:
        raise DockerSimulatorAcceptanceError("scenario runtime_input path is invalid")
    if not isinstance(runtime_sha, str) or len(runtime_sha) != 64:
        raise DockerSimulatorAcceptanceError("scenario runtime_input hash is invalid")
    runtime_path = (scenario_path.parent / runtime_name).resolve(strict=True)
    if runtime_path.is_symlink() or not runtime_path.is_file():
        raise DockerSimulatorAcceptanceError("scenario runtime_input must be a regular file")
    try:
        runtime_document = json.loads(runtime_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DockerSimulatorAcceptanceError("scenario runtime_input is not valid JSON") from exc
    if not isinstance(runtime_document, Mapping):
        raise DockerSimulatorAcceptanceError("scenario runtime_input must be an object")
    if canonical_sha256(runtime_document) != runtime_sha:
        raise DockerSimulatorAcceptanceError("scenario runtime_input canonical hash mismatch")
    seed = runtime_document.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise DockerSimulatorAcceptanceError("scenario runtime_input seed is invalid")
    return seed


def run_acceptance(
    scenario_path: str | Path = DEFAULT_SCENARIO,
    evidence_path: str | Path = DEFAULT_EVIDENCE,
    *,
    model_path: str | Path | None = None,
    model_id: str | None = None,
    model_version: str | None = None,
    execute: bool = False,
    timeout_s: float = 90.0,
    docker: str | Path | None = None,
    container_name: str = DEFAULT_CONTAINER_NAME,
    control_port: int = DEFAULT_CONTROL_PORT,
    telemetry_port: int = DEFAULT_TELEMETRY_PORT,
    image: str = PINNED_IMAGE,
    runner: Callable[..., object] | None = None,
    sensor_frame_count: int = 0,
    sensor_width: int = 1280,
    sensor_height: int = 960,
    sensor_frame_output_dir: str | Path | None = None,
    sensor_depth_output_dir: str | Path | None = None,
    use_actual_detector: bool = False,
    detector_model_path: str | Path = DEFAULT_DETECTOR_MODEL,
    execute_mission_transactions: bool = False,
    mission_journal_directory: str | Path | None = None,
    execute_autonomous_flight: bool = False,
    autonomous_flight_timeout_s: float = 15.0,
    capture_plan_path: str | Path | None = None,
    recording_output_directory: str | Path | None = None,
    recording_id: str | None = None,
    recording_scenario_seed: int | None = None,
) -> dict[str, object]:
    source = Path(scenario_path).expanduser().resolve()
    evidence = Path(evidence_path).expanduser().resolve()
    recording_output = (
        None
        if recording_output_directory is None
        else Path(recording_output_directory).expanduser().resolve()
    )
    if recording_output is not None and not sensor_frame_output_dir:
        raise DockerSimulatorAcceptanceError(
            "recording output requires --sensor-frame-output-dir"
        )
    if recording_output is not None and not sensor_depth_output_dir:
        raise DockerSimulatorAcceptanceError(
            "recording output requires --sensor-depth-output-dir"
        )
    if recording_output is not None and sensor_frame_count < 2:
        raise DockerSimulatorAcceptanceError(
            "recording output requires at least two Gazebo sensor frames"
        )
    capture_plan: dict[str, Any] | None = None
    if capture_plan_path is not None:
        try:
            scenario_document = json.loads(source.read_text(encoding="utf-8"))
            scenario_id = str(scenario_document.get("scenario_id", "")).strip()
        except (OSError, json.JSONDecodeError, AttributeError, TypeError) as exc:
            raise DockerSimulatorAcceptanceError(
                f"capture plan requires a readable scenario document: {source}"
            ) from exc
        if not scenario_id:
            raise DockerSimulatorAcceptanceError("scenario document has no scenario_id")
        capture_plan = load_capture_plan(
            Path(capture_plan_path),
            scenario_id=scenario_id,
            vehicle_class="multirotor",
        )
        current_manifest = ROOT / "configs" / "scenarios" / "gazebo-3d-mainline-v1.manifest.json"
        if capture_plan.get("manifest_sha256") != _sha256(current_manifest):
            raise DockerSimulatorAcceptanceError(
                "capture plan is stale: its bound Gazebo 3-D manifest does not "
                "match the current manifest"
            )
    if model_path is None:
        reference = load_onboard_model_reference(
            DEFAULT_MODEL_REFERENCE,
            repository_root=ROOT,
        )
        model = reference.model_path
        model_id = model_id or reference.model_id
        model_version = model_version or reference.model_version
    else:
        model = Path(model_path).expanduser().resolve()
    if not model.is_file():
        raise DockerSimulatorAcceptanceError(f"OnboardAI model file does not exist: {model}")
    model_sha256 = _sha256(model)
    model_suffix = model.stem.removeprefix("onboard_policy_")
    model_identity_id = model_id or f"onboard-policy-{model_suffix}"
    model_identity_version = model_version or f"{PRODUCT_VERSION.release_label}-{model_identity_id}"
    detector_model = Path(detector_model_path).expanduser().resolve()
    if use_actual_detector and sensor_frame_count < 2:
        raise DockerSimulatorAcceptanceError(
            "actual detector mode requires at least two Gazebo sensor frames"
        )
    if execute_autonomous_flight and not execute:
        raise DockerSimulatorAcceptanceError("autonomous flight requires --execute")
    if execute_autonomous_flight and not execute_mission_transactions:
        raise DockerSimulatorAcceptanceError(
            "autonomous flight requires --execute-mission-transactions"
        )
    # This runner connects only to the Docker-published loopback endpoint.
    # Keep the explicit guard here so the opt-in cannot silently become a
    # real-hardware arm path if the transport is refactored later.
    if execute_autonomous_flight and not (1 <= int(control_port) <= 65_535):
        raise DockerSimulatorAcceptanceError(
            "autonomous flight requires a valid loopback Docker port"
        )
    plan = build_docker_simulator_plan(
        source,
        repository_root=ROOT,
        job_dir=evidence.parent / f"{container_name}-job",
        container_name=container_name,
        control_port=control_port,
        telemetry_port=telemetry_port,
        image=image,
        dry_run=not execute,
    )
    airframe_hash = _airframe_profile_hash(plan)
    if not execute:
        result = {
            "schema": "multidetect.external-onboard-model-acceptance",
            "schema_version": 1,
            "status": "planned_not_executed",
            "release_label": PRODUCT_VERSION.release_label,
            "scenario": str(source),
            "docker_plan_sha256": plan.evidence_hash,
            "model_path": str(model),
            "model_sha256": model_sha256,
            "model_id": model_identity_id,
            "model_version": model_identity_version,
            "airframe_profile_sha256": airframe_hash,
            "external_gazebo_software_sim": False,
            "truth_free": True,
            "synthetic_structured_perception": not use_actual_detector,
            "actual_rgb_detector_used": False,
            "actual_detector_model_path": str(detector_model),
            "actual_detector_model_sha256": (
                _sha256(detector_model)
                if use_actual_detector and detector_model.is_file()
                else None
            ),
            "actual_detector_confidence_threshold": (
                ACTUAL_DETECTOR_CONFIDENCE_THRESHOLD if use_actual_detector else None
            ),
            "actionable_detector_confidence_threshold": (
                MIN_TARGET_DETECTOR_CONFIDENCE if use_actual_detector else None
            ),
            "visual_accuracy": "not_claimed",
            "hil_real_evidence": False,
            "jetson_hardware_evidence": False,
            "outdoor_gnss_evidence": False,
            "final_release_allowed": False,
            "autonomous_flight_enabled": False,
            "capture_plan": None
            if capture_plan is None
            else {
                "path": capture_plan["path"],
                "sha256": capture_plan["sha256"],
                "scenario_id": capture_plan["scenario_id"],
                "vehicle_class": capture_plan["vehicle_class"],
                "target_pose_excluded_from_runtime": True,
                "target_pose_excluded_from_model_input": True,
                "target_pose_used_for_capture_route": False,
                "route_is_initial_mission_geometry_only": True,
            },
            "recording_binding": {
                "requested": recording_output is not None,
                "status": (
                    "planned_after_capture" if recording_output is not None else "not_requested"
                ),
                "output_directory": None if recording_output is None else str(recording_output),
            },
        }
    else:
        probes: list[ExternalOnboardDockerProbe] = []
        sensor_frames: list[tuple[object, object, object | None]] = []
        sensor_capture_start_event = (
            threading.Event() if execute_autonomous_flight and use_actual_detector else None
        )
        frame_perception_adapter = (
            _build_actual_perception_adapter(detector_model) if use_actual_detector else None
        )

        def on_sensor_frame(frame: object, provenance: object, depth: object | None) -> None:
            sensor_frames.append((frame, provenance, depth))

        def factory(endpoint: str) -> ExternalOnboardDockerProbe:
            run_journal_directory = (
                Path(mission_journal_directory).expanduser().resolve()
                if mission_journal_directory is not None
                else evidence.parent / f"{evidence.stem}-mission-journal"
            )
            probe = ExternalOnboardDockerProbe(
                endpoint,
                model_path=model,
                model_sha256=model_sha256,
                airframe_hash=airframe_hash,
                model_id=model_identity_id,
                model_version=model_identity_version,
                sensor_frames=sensor_frames,
                frame_perception_adapter=frame_perception_adapter,
                execute_mission_transactions=execute_mission_transactions,
                mission_journal_directory=run_journal_directory,
                execute_autonomous_flight=execute_autonomous_flight,
                autonomous_flight_timeout_s=autonomous_flight_timeout_s,
                sensor_capture_start_event=sensor_capture_start_event,
                capture_route=None if capture_plan is None else capture_plan["route"],
            )
            if execute_autonomous_flight:
                # Reuse the existing disposable SITL arm preparation.  This
                # changes only the freshly-created PX4/Gazebo container and
                # keeps the host/Pixhawk path out of the opt-in lifecycle.
                probe.arm_preflight = _prepare_disposable_sitl_for_arm(
                    _resolve_docker_executable(docker),
                    container_name,
                    runner=runner,
                    enable_joystick_control=False,
                )
            probes.append(probe)
            return probe

        base = run_docker_acceptance(
            source,
            evidence,
            execute=True,
            timeout_s=timeout_s,
            docker=docker,
            container_name=container_name,
            control_port=control_port,
            telemetry_port=telemetry_port,
            image=image,
            sensor_frame_count=sensor_frame_count,
            sensor_width=sensor_width,
            sensor_height=sensor_height,
            sensor_frame_output_dir=sensor_frame_output_dir,
            sensor_depth_output_dir=sensor_depth_output_dir,
            sensor_frame_callback=on_sensor_frame,
            runner=runner,
            telemetry_probe_factory=factory,
            sensor_capture_concurrent=sensor_capture_start_event is not None,
            sensor_capture_start_event=sensor_capture_start_event,
        )
        recording_result: dict[str, object] | None = None
        if recording_output is not None:
            rgb_manifest = (
                Path(sensor_frame_output_dir).expanduser().resolve()
                / "gazebo-bgr24-frame-set.manifest.json"
            )
            depth_manifest = (
                Path(sensor_depth_output_dir).expanduser().resolve()
                / "gazebo-depth-companion-set.manifest.json"
            )
            try:
                scenario_document = json.loads(source.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise DockerSimulatorAcceptanceError(
                    f"recording binding requires a readable scenario document: {source}"
                ) from exc
            scenario_seed = recording_scenario_seed
            if scenario_seed is None:
                scenario_seed = _scenario_seed_from_runtime(source, scenario_document)
            recording_result = build_gazebo_raw_recording(
                rgb_manifest,
                depth_manifest,
                recording_output,
                recording_id=recording_id or evidence.stem,
                airframe_profile_hash=airframe_hash,
                scenario_seed=scenario_seed,
            )
        probe = probes[-1] if probes else None
        result = {
            "schema": "multidetect.external-onboard-model-acceptance",
            "schema_version": 1,
            "status": (
                "passed"
                if probe is not None
                and isinstance(probe.observe_result, Mapping)
                and probe.observe_result.get("status") == "passed"
                else "failed"
            ),
            "release_label": PRODUCT_VERSION.release_label,
            "scenario": str(source),
            "docker_plan_sha256": plan.evidence_hash,
            "model_path": str(model),
            "model_sha256": model_sha256,
            "model_id": model_identity_id,
            "model_version": model_identity_version,
            "airframe_profile_sha256": airframe_hash,
            "external_gazebo_software_sim": True,
            "base_docker_acceptance": base,
            "probe": None if probe is None else probe.observe_result,
            "onboard": None
            if probe is None
            else {
                "bridge": None if probe.bridge is None else probe.bridge.evidence(),
                "installed_mission_count": len(probe.installed_missions),
                "safety_request_count": len(probe.safety_requests),
                "safety_requests": [
                    item.to_mapping() if hasattr(item, "to_mapping") else str(item)
                    for item in probe.safety_requests
                ],
                "safety_dispatch_errors": list(probe.safety_dispatch_errors),
                "synthetic_structured_perception": not use_actual_detector,
                "actual_rgb_detector_used": use_actual_detector,
                "mission_transport_enabled": execute_mission_transactions,
                "autonomous_flight_enabled": execute_autonomous_flight,
                "actual_detector_model_path": str(detector_model),
                "actual_detector_model_sha256": (
                    _sha256(detector_model) if use_actual_detector else None
                ),
                "actual_detector_confidence_threshold": (
                    ACTUAL_DETECTOR_CONFIDENCE_THRESHOLD if use_actual_detector else None
                ),
                "actionable_detector_confidence_threshold": (
                    MIN_TARGET_DETECTOR_CONFIDENCE if use_actual_detector else None
                ),
                "visual_accuracy": "not_claimed",
                "policy_applied_to_actuator": False,
                "gazebo_sensor_frame_count": len(sensor_frames),
                "stale_sensor_frames_rejected": probe.stale_sensor_frames_rejected,
            },
            "truth_free": True,
            "hil_real_evidence": False,
            "jetson_hardware_evidence": False,
            "outdoor_gnss_evidence": False,
            "final_release_allowed": False,
            "autonomous_flight_enabled": execute_autonomous_flight,
            "capture_plan": None
            if capture_plan is None
            else {
                "path": capture_plan["path"],
                "sha256": capture_plan["sha256"],
                "scenario_id": capture_plan["scenario_id"],
                "vehicle_class": capture_plan["vehicle_class"],
                    "target_pose_excluded_from_runtime": True,
                    "target_pose_excluded_from_model_input": True,
                    "target_pose_used_for_capture_route": False,
                "route_is_initial_mission_geometry_only": True,
            },
            "recording_binding": {
                "requested": recording_output is not None,
                "status": "created" if recording_result is not None else "not_requested",
                "output_directory": None if recording_output is None else str(recording_output),
                "result": recording_result,
            },
        }
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, default=DEFAULT_SCENARIO)
    parser.add_argument(
        "--model",
        type=Path,
        help="explicit ONNX path; omitted means the hash-bound current reference",
    )
    parser.add_argument(
        "--model-reference",
        type=Path,
        help=(
            "load the hash-bound selected OnboardAI reference; this overrides "
            "--model for the SIM runner"
        ),
    )
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument("--container-name", default=DEFAULT_CONTAINER_NAME)
    parser.add_argument("--control-port", type=int, default=DEFAULT_CONTROL_PORT)
    parser.add_argument("--telemetry-port", type=int, default=DEFAULT_TELEMETRY_PORT)
    parser.add_argument("--image", default=PINNED_IMAGE)
    parser.add_argument("--docker", type=Path)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--sensor-frames", type=int, default=0)
    parser.add_argument("--sensor-width", type=int, default=1280)
    parser.add_argument("--sensor-height", type=int, default=960)
    parser.add_argument("--sensor-frame-output-dir", type=Path)
    parser.add_argument("--sensor-depth-output-dir", type=Path)
    parser.add_argument(
        "--recording-output-directory",
        type=Path,
        help=(
            "after a successful modern RGB/Depth capture, bind it to the existing "
            "raw replay/OnboardAI training contract"
        ),
    )
    parser.add_argument("--recording-id")
    parser.add_argument("--recording-scenario-seed", type=int)
    parser.add_argument(
        "--use-actual-detector",
        action="store_true",
        help="run the candidate YOLO detector on captured Gazebo RGB frames",
    )
    parser.add_argument("--detector-model", type=Path, default=DEFAULT_DETECTOR_MODEL)
    parser.add_argument(
        "--execute-mission-transactions",
        action="store_true",
        help=(
            "execute hash-bound PX4 Mission upload/readback/commit/permit for each "
            "model plan; this does not arm or start flight"
        ),
    )
    parser.add_argument(
        "--execute-autonomous-flight",
        action="store_true",
        help=(
            "opt in to the disposable loopback SITL lifecycle: AUTO.MISSION, "
            "ARM, model-driven RTL and telemetry-gated report; requires "
            "--execute --execute-mission-transactions"
        ),
    )
    parser.add_argument(
        "--autonomous-flight-timeout",
        type=float,
        default=15.0,
        help="seconds to wait for the armed PX4 heartbeat after the explicit ARM request",
    )
    parser.add_argument("--mission-journal-directory", type=Path)
    parser.add_argument(
        "--capture-plan",
        type=Path,
        help=(
            "offline Gazebo capture route generated from the 3-D manifest; target pose "
            "is never passed to OnboardAI"
        ),
    )
    parser.add_argument("--execute", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        model_id = None
        model_version = None
        model_path = args.model
        if args.model_reference is not None:
            reference = load_onboard_model_reference(
                args.model_reference,
                repository_root=ROOT,
            )
            model_path = reference.model_path
            model_id = reference.model_id
            model_version = reference.model_version
        elif model_path is None:
            reference = load_onboard_model_reference(
                DEFAULT_MODEL_REFERENCE,
                repository_root=ROOT,
            )
            model_path = reference.model_path
            model_id = reference.model_id
            model_version = reference.model_version
        result = run_acceptance(
            args.scenario,
            args.evidence,
            model_path=model_path,
            model_id=model_id,
            model_version=model_version,
            execute=args.execute,
            timeout_s=args.timeout,
            docker=args.docker,
            container_name=args.container_name,
            control_port=args.control_port,
            telemetry_port=args.telemetry_port,
            image=args.image,
            sensor_frame_count=args.sensor_frames,
            sensor_width=args.sensor_width,
            sensor_height=args.sensor_height,
            sensor_frame_output_dir=args.sensor_frame_output_dir,
            sensor_depth_output_dir=args.sensor_depth_output_dir,
            recording_output_directory=args.recording_output_directory,
            recording_id=args.recording_id,
            recording_scenario_seed=args.recording_scenario_seed,
            use_actual_detector=args.use_actual_detector,
            detector_model_path=args.detector_model,
            execute_mission_transactions=args.execute_mission_transactions,
            mission_journal_directory=args.mission_journal_directory,
            execute_autonomous_flight=args.execute_autonomous_flight,
            autonomous_flight_timeout_s=args.autonomous_flight_timeout,
            capture_plan_path=args.capture_plan,
        )
    except (DockerSimulatorAcceptanceError, OSError, RuntimeError, TypeError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("status") in {"passed", "planned_not_executed"} else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
