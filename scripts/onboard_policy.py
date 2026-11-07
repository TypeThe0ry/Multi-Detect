"""Shared truth-free policy for the SIM teacher and real onboard runtime.

FAST: a fine-tuned YOLOE model with a generic open vocabulary proposes candidate
boxes at low confidence (class labels are ignored). SLOW: the quantized
Qwen3-VL slow system (llama-server) judges whether a tracked candidate is the
task's object. The host loop (``multidetect.autonomy.onboard_loop``) decides
approach, confirmation by apparent size, and the return home. The policy only
receives the task text, camera frames, the vehicle pose and the static world
map used for search legs and obstacle clearance; it never reads scenario truth.
"""

from __future__ import annotations

import hashlib
from contextlib import nullcontext
import io
import json
import math
import os
import sys
import threading
import time
import warnings
from dataclasses import asdict
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.energy_estimator import EnergyConfig  # noqa: E402
from multidetect.autonomy.capability_contract import (  # noqa: E402
    capabilities_for_airframe,
    effective_capabilities,
    normalize_capabilities,
)
from multidetect.autonomy.fw_search import FixedWingSearch, camera_footprint_width_m  # noqa: E402
from multidetect.autonomy.ground_model import ground_projection_from_config  # noqa: E402
from multidetect.autonomy.mission_executor import (  # noqa: E402
    ExecutorConfig,
    MissionExecutor,
    plan_for_count_task,
    plan_for_find_and_inspect,
    plan_for_find_task,
    task_text_for,
)
from multidetect.autonomy.mission_package import (  # noqa: E402
    MissionPackage,
    check_plan_against_package,
    load_mission_package,
)
from multidetect.autonomy.mission_plan import (  # noqa: E402
    MissionPlan,
    MissionStep,
    PlanDecision,
    validate_mission_plan,
)
from multidetect.autonomy.plan_pregate import preflight_plan_task  # noqa: E402
from multidetect.autonomy.mission_planner import (  # noqa: E402
    compile_mission_plan,
    plan_json_schema,
)
from multidetect.autonomy.mission_summary import summarize_mission, summary_json  # noqa: E402
from multidetect.autonomy.mission_supervisor import MissionSupervisor  # noqa: E402
from multidetect.autonomy.motion_intent import (  # noqa: E402
    AirframeMotionAdapter,
    MotionCommand,
    camera_ray_from_profile,
    profile_mount_pitch_down_rad,
)
from multidetect.autonomy.near_depth import NearDepthConfig, NearDepthController  # noqa: E402
from multidetect.autonomy.obstacle_source import CompositeSource, StaticMapSource  # noqa: E402
from multidetect.autonomy.onboard_decision import identity_of  # noqa: E402
from multidetect.autonomy.onboard_loop import (  # noqa: E402
    CameraModel,
    Detection,  # noqa: F401 - retained as a compatibility export
    LoopConfig,
    OnboardLoop,
)
from multidetect.autonomy.onboard_mission_log import (  # noqa: E402
    DownlinkQueue,
    OnboardMissionLog,
)
from multidetect.autonomy.onboard_policy_parts.assembly import (  # noqa: E402
    assemble_runtime_executor as _assemble_runtime_executor,
    assemble_runtime_runners as _assemble_runtime_runners,
    build_runtime_loop_factory as _build_runtime_loop_factory,
)
from multidetect.autonomy.onboard_policy_parts.config import (  # noqa: E402
    DEFAULTS,
    POSITION_DEFAULTS,
)
from multidetect.autonomy.onboard_policy_parts.helpers import (  # noqa: E402
    _enabled,
    _observation_timestamp_s,
    _vehicle_receipt_s,
    labelled_refs,
    perception_task_text as _perception_task_text,
)
from multidetect.autonomy.onboard_policy_parts.proposer import YoloeProposer  # noqa: E402
from multidetect.autonomy.onboard_policy_parts.runtime import (  # noqa: E402
    RuntimeAdmissionMixin,
    build_safety_envelope as _build_safety_envelope,
    load_runtime_mission_package,
    prepare_runtime_operator_package as _prepare_runtime_operator_package,
)
from multidetect.autonomy.onboard_policy_parts.simulation import (  # noqa: E402
    SimGroundTimeline,
    validate_sim_ground_events,
)
from multidetect.autonomy.onboard_policy_parts.vocabulary import VOCABULARY  # noqa: E402
from multidetect.autonomy.plan_validation import validate_plan_for_execution  # noqa: E402
from multidetect.autonomy.safety_envelope import SafetyEnvelope, SafetyEnvelopeConfig  # noqa: E402
from multidetect.onboard_runtime.health import (  # noqa: E402
    HealthConfig,
    HealthDecision,
    HealthMonitor,
)
from multidetect.sim_teacher.policies import (  # noqa: E402
    ExpertSearchPolicy,
    load_sdf_obstacles,
    segment_clear_of_obstacles,
)
from multidetect.sim_teacher.primitives import ActionPrimitive  # noqa: E402
from multidetect.simulator.camera_projection import (  # noqa: E402
    pixel_camera_ray,
    pixel_ground_point,
)
from multidetect.simulator.task_semantics import typical_object_size  # noqa: E402


from multidetect.autonomy.onboard_policy_parts.model_config import (  # noqa: E402
    _model_sha256 as _model_sha256_impl,
    _planning_model_spec as _planning_model_spec_impl,
)


MEMORY = "No prior confirmed target; search in progress."
MAX_PROMPT_CANDIDATES = 4


def _model_sha256(path: str | Path, *, cache_path: Path | None = None) -> str:
    return _model_sha256_impl(
        path, cache_path=cache_path, path_type=Path, sha256_factory=hashlib.sha256
    )


def _planning_model_spec(value: Any, *, identity_mmproj: str) -> tuple[str, str | None] | None:
    return _planning_model_spec_impl(
        value, identity_mmproj=identity_mmproj, root=ROOT, path_type=Path
    )


def perception_task_text(step, original: str) -> str:
    return _perception_task_text(step, original, task_text_for=task_text_for)


def build_mission_from_text(
    task_text: str,
    refs: Mapping[str, Any] | Sequence[str] | None,
    post: Callable[[str, dict], str],
    sorted_keys: bool = True,
    *,
    plan_checker: Callable[[MissionPlan], Sequence[Mapping[str, Any]]] | None = None,
    attempts: int = 2,
    capabilities: frozenset[str] = frozenset(),
    airframe: Any = None,
    include_decision_contract: bool = False,
    enable_plan_pregate: bool = False,
) -> tuple[MissionPlan | PlanDecision, dict[str, Any]]:
    """Compile a task; explicit reject/clarify results never get a flight fallback."""
    if enable_plan_pregate:
        decision = preflight_plan_task(task_text, refs, capabilities, airframe)
        if decision is not None:
            return decision, {
                "error": None,
                "decision": decision,
                "source": "host_pregate",
                "raw": None,
                "attempts": [],
                "repairs": [],
            }
    schema = plan_json_schema(
        capabilities=capabilities, include_decision_contract=include_decision_contract
    )
    if sorted_keys:
        from scripts.slow_system_runtime import trained_key_order_schema

        schema = trained_key_order_schema(schema)
    ref_names = tuple(refs or ())
    plan, report = compile_mission_plan(
        post,
        task_text,
        refs=ref_names,
        schema=schema,
        attempts=attempts,
        plan_checker=plan_checker,
        capabilities=capabilities,
        airframe=airframe,
        include_decision_contract=include_decision_contract,
    )
    if isinstance(plan, PlanDecision):
        return plan, report
    if plan is None:
        plan = validate_mission_plan(
            {
                "plan_version": 1,
                "goal": "return home safely",
                "steps": [
                    {
                        "id": "home",
                        "skill": "return_home",
                        "params": {},
                        "on_success": None,
                        "on_fail": None,
                    }
                ],
                "report": ["status"],
            }
        )
    return plan, report


def _runtime_runner_type() -> Callable[..., Any]:
    from scripts.eval_slow_system import LlamaServerRunner

    return LlamaServerRunner


def _runtime_http_json() -> Callable[..., Any]:
    from scripts.eval_slow_system import _http_json

    return _http_json


class OnboardPolicy(RuntimeAdmissionMixin):
    # Optional subsystems default to "absent" (tests build bare instances).
    supervisor: MissionSupervisor | None = None
    downlink: DownlinkQueue | None = None
    sim_timeline: SimGroundTimeline | None = None
    mission_plan: MissionPlan | None = None
    ground_projection = ground_projection_from_config(None, base_dir=ROOT)
    frame_format = "png"

    def __init__(
        self,
        *,
        task_text: str,
        world_path: Path | None,
        altitude_m: float,
        camera_info: dict[str, float],
        image_size: tuple[int, int],
        work_dir: Path,
        config: dict[str, Any] | None = None,
        proposer: Callable | None = None,
        slow_runner: Any = None,
        ground_projector: Callable | None = None,
        home_ned: tuple[float, float, float] | None = None,
        runtime_mode: str | None = None,
        frame_format: str = "png",
        frame_quality: int = 90,
        mission_package: MissionPackage | None = None,
    ) -> None:
        options = {
            **{k: (str(v) if v is not None else None) for k, v in DEFAULTS.items()},
            **(config or {}),
        }
        if type(options.get("planning_decision_contract", False)) is not bool:
            raise ValueError("planning_decision_contract must be a boolean")
        if type(options.get("planning_pregate", False)) is not bool:
            raise ValueError("planning_pregate must be a boolean")
        # Profile resolution is deliberately early: an unknown aircraft must
        # fail closed before models, mission plans, or vehicle I/O are started.
        self.motion_adapter = AirframeMotionAdapter(
            options.get("airframe", "x500_mono_cam"), base_dir=ROOT
        )
        self.airframe_profile = self.motion_adapter.profile
        # A VTOL can hover: search, approach and confirmation fly the multirotor
        # loop in MC (the loop's level-camera model, turn/step approach and
        # closest view), and the confirmed stop hands the return to the autopilot.
        raw_hover_ops = options.get("vtol_hover_ops", True)
        if isinstance(raw_hover_ops, str):
            if raw_hover_ops.strip().lower() not in {"true", "false"}:
                raise ValueError("vtol_hover_ops must be true or false")
            raw_hover_ops = raw_hover_ops.strip().lower() == "true"
        elif type(raw_hover_ops) is not bool:
            raise ValueError("vtol_hover_ops must be a boolean")
        if (
            raw_hover_ops
            and self.airframe_profile.category == "vtol"
            and self.airframe_profile.can_hover
        ):
            self.motion_adapter.preferred_mode = "MC"
        hover_ops = self.motion_adapter.hover_ops
        raw_search_mode = options.get("vtol_search_mode", "mc")
        search_mode = str(raw_search_mode).strip().lower()
        if search_mode not in {"mc", "fw"}:
            raise ValueError("vtol_search_mode must be mc or fw")
        if search_mode == "fw" and not (
            raw_hover_ops
            and self.airframe_profile.category == "vtol"
            and self.airframe_profile.can_hover
        ):
            raise ValueError("vtol_search_mode=fw requires a hover-capable VTOL")
        hybrid_vtol = (
            search_mode == "fw"
            and raw_hover_ops
            and self.airframe_profile.category == "vtol"
            and self.airframe_profile.can_hover
        )
        self.vtol_search_mode = search_mode
        self._hybrid_vtol = hybrid_vtol
        if hybrid_vtol:
            self.motion_adapter.preferred_mode = "FW"
        self._hybrid_mc_engaged = False
        self._hybrid_mc_search_steps = 0
        self._hybrid_rejections_seen = 0
        self._hybrid_transition_count = 0
        self._hybrid_candidate_visible = False
        self._hybrid_fw_reentry_backoff_until = -1
        self._hybrid_step_token = None
        # Hover-mode VTOLs plan like a multirotor; only their commands differ.
        plans_like_multirotor = self.motion_adapter.is_multirotor or hover_ops
        effective_runtime_mode = runtime_mode or options.get("runtime_mode")
        self.config = options
        self.runtime_mode = effective_runtime_mode
        sim_events = validate_sim_ground_events(options)
        self.sim_timeline = (
            SimGroundTimeline(sim_events) if "sim_ground_events" in options else None
        )
        (
            self.mission_package,
            self._ground_key_setting,
            self.airframe_capabilities,
            effective_airframe_capabilities,
        ) = load_runtime_mission_package(
            options,
            runtime_mode=effective_runtime_mode,
            mission_package=mission_package,
            altitude_m=altitude_m,
            airframe_profile=self.airframe_profile,
            enabled=_enabled,
            root=ROOT,
        )
        self.ground_projection = ground_projection_from_config(
            options.get("ground_model"), base_dir=ROOT
        )
        self.safety_envelope = _build_safety_envelope(
            options,
            self.airframe_profile,
            self.motion_adapter,
            self.mission_package,
            home_ned,
            self.ground_projection,
        )
        # Keep the adapter on the same envelope instance so a rejection has a
        # single counter/event stream and fixed-wing recovery becomes loiter.
        self.motion_adapter.safety = self.safety_envelope
        for key in ("yoloe_weights", "gguf", "mmproj", "llama_server"):
            # Config paths are relative to the repository root.
            path = Path(options[key])
            options[key] = str(path if path.is_absolute() else ROOT / path)
        raw_near_depth = options.get("near_depth", False)
        if isinstance(raw_near_depth, str):
            if raw_near_depth.strip().lower() not in {"true", "false"}:
                raise ValueError("near_depth must be true or false")
            near_depth_enabled = raw_near_depth.strip().lower() == "true"
        elif type(raw_near_depth) is bool:
            near_depth_enabled = raw_near_depth
        else:
            raise ValueError("near_depth must be a boolean")
        near_model_path = Path(
            str(options.get("near_depth_model_path", DEFAULTS["near_depth_model_path"]))
        )
        if not near_model_path.is_absolute():
            near_model_path = ROOT / near_model_path
        near_depth_config = NearDepthConfig(
            enabled=near_depth_enabled,
            model_path=near_model_path,
            backend=str(options.get("near_depth_backend", "onnx-cpu")),
            max_range_m=float(options.get("near_depth_max_range_m", 10.0)),
            min_interval_s=float(options.get("near_depth_min_interval_s", 0.2)),
            max_age_s=float(options.get("near_depth_max_age_s", 1.0)),
            sigma_scale=float(options.get("near_depth_sigma_scale", 1.0)),
        )
        # Construction is deliberately lazy: disabled mode does not import a
        # backend or touch the model artifact.
        self.near_depth = NearDepthController(near_depth_config)
        # An operator command from the config overrides the world's task sentence.
        self.task_text = str(options.get("task_text") or task_text)
        self.work_dir = work_dir
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.image_size = image_size
        self._search_camera_pose: Mapping[str, object] | None = None
        if frame_format not in {"png", "jpeg"}:
            raise ValueError("frame_format must be png or jpeg")
        self.frame_format = frame_format
        self.frame_quality = int(frame_quality)
        self.ground_projector = ground_projector
        make_loop = _build_runtime_loop_factory(
            self,
            options=options,
            task_text=task_text,
            world_path=world_path,
            altitude_m=altitude_m,
            camera_info=camera_info,
            image_size=image_size,
            home_ned=home_ned,
            hover_ops=hover_ops,
            hybrid_vtol=hybrid_vtol,
            plans_like_multirotor=plans_like_multirotor,
            near_depth_enabled=near_depth_enabled,
            position_defaults=POSITION_DEFAULTS,
        )
        mission = options.get("mission")
        if mission not in {
            None,
            "find",
            "count",
            "find_and_inspect",
            "from_text",
            "package",
            "operator_link",
        }:
            raise ValueError(f"unknown mission template {mission!r}")
        self._step = 0
        # Keep facade bindings live during later restart and planning callbacks.
        runner_state = _assemble_runtime_runners(
            self,
            options=options,
            mission=mission,
            proposer=proposer,
            slow_runner=slow_runner,
            proposer_factory=lambda *args, **kwargs: YoloeProposer(*args, **kwargs),
            health_monitor_factory=lambda **kwargs: HealthMonitor(**kwargs),
            path_type=lambda *args: Path(*args),
            planning_model_spec=lambda *args, **kwargs: _planning_model_spec(*args, **kwargs),
            model_sha256=lambda *args, **kwargs: _model_sha256(*args, **kwargs),
            load_runner_type=_runtime_runner_type,
        )
        _assemble_runtime_executor(
            self,
            options=options,
            mission=mission,
            capabilities=effective_airframe_capabilities,
            make_loop=make_loop,
            altitude_m=altitude_m,
            home_ned=home_ned,
            runtime_mode=effective_runtime_mode,
            runner_state=runner_state,
            build_mission_from_text=lambda *args, **kwargs: build_mission_from_text(*args, **kwargs),
            perception_task_text=lambda *args: perception_task_text(*args),
            load_http_json=_runtime_http_json,
            enabled=_enabled,
        )

    def admit_operator_package(self, package: MissionPackage) -> MissionSupervisor:
        """Compile and install a package (both steps; tests and legacy hosts)."""
        supervisor = self.prepare_operator_package(package)
        self.install_operator_package(supervisor)
        return supervisor

    def prepare_operator_package(self, package: MissionPackage) -> MissionSupervisor:
        """Compile and check a signed operator-link package without installing it.

        Runs on the operator link's planner worker.  The running (or waiting)
        mission is untouched; ``MissionOperatorAdapter`` installs the returned
        candidate through :meth:`install_operator_package` only after its own
        checks, a fresh preflight read and setting the plan-approval gate.
        """
        return _prepare_runtime_operator_package(
            self,
            package,
            build_mission_from_text=lambda *args, **kwargs: build_mission_from_text(*args, **kwargs),
            perception_task_text=lambda *args: perception_task_text(*args),
            path_type=lambda *args: Path(*args),
            load_runner_type=_runtime_runner_type,
            load_http_json=_runtime_http_json,
        )

    def close(self) -> None:
        self._model_shutdown.set()
        self._cancel_slow_restart(wait=True)
        try:
            if self.supervisor is not None:
                self.supervisor.finish()
            if self.downlink is not None:
                self.downlink.close()
        finally:
            try:
                with self._model_lock:
                    close = getattr(self.slow, "close", None)
                    if close is not None:  # injected runners may not own a process
                        close()
                    self.slow = None
                    result = self._slow_restart_result
                    self._slow_restart_result = None
                    close = getattr(result, "close", None)
                    if callable(close):
                        close()
            finally:
                if self.executor is not None:
                    try:
                        self._write_mission_summary()
                    except OSError as exc:
                        warnings.warn(f"Could not write mission summary: {exc}", stacklevel=2)
                near_depth = getattr(self, "near_depth", None)
                if near_depth is not None:
                    near_depth.close()

    def _cancel_slow_restart(self, *, wait: bool = True) -> None:
        lock = getattr(self, "_model_lock", None)
        context = lock if lock is not None else nullcontext()
        with context:
            self._slow_restart_generation = getattr(self, "_slow_restart_generation", 0) + 1
            thread = getattr(self, "_slow_restart_thread", None)
        if thread is not None and thread.is_alive() and wait:
            thread.join(timeout=max(0.0, getattr(self, "_slow_restart_shutdown_timeout_s", 5.0)))
        with context:
            if thread is not None and not thread.is_alive():
                self._slow_restart_thread = None
            result = getattr(self, "_slow_restart_result", None)
            self._slow_restart_result = None
        close = getattr(result, "close", None)
        if callable(close):
            close()

    def ground_event(self, event: dict[str, Any]) -> bool:
        """Optional transport adapter: timestamps are local receipt times."""
        return self.supervisor.ground_event(event) if self.supervisor is not None else False

    def report_primitive_outcome(self, action, status: str, *, before, after) -> bool:
        """Feed PX4 execution evidence back to active return-home recovery."""
        supervisor = self.supervisor
        executor = (
            getattr(supervisor, "_recovery", None)
            if supervisor is not None and getattr(supervisor, "action", None) in {"return_now", "land_now"}
            else self.executor
        )
        check = getattr(executor, "report_primitive_outcome", None)
        if not callable(check) or not check(action, status, before=before, after=after):
            return False
        if supervisor is not None:
            supervisor.log.append(
                "climb_failed_suspected_loss_of_control",
                supervisor.clock(),
                sim_time_s=float((after.get("pose_time_boot_ms") or 0)) / 1000.0,
                height_m=max(0.0, -float(after["pose"]["local_ned_m"][2])),
            )
            supervisor._escalate("land_now", "climb_failed_suspected_loss_of_control")
        return True

    def set_downlink_sender(self, sender: Callable[[dict[str, Any]], object] | None) -> None:
        """Attach an optional acknowledged transport; delivery runs in the queue worker."""
        if self.downlink is not None:
            self.downlink.set_sender(sender)

    def _write_mission_summary(self) -> None:
        """Persist at teardown, beside the collector's steps.jsonl labels."""
        executor = self.executor
        report = executor.report()
        report_input = {
            "history": list(executor.history),
            "findings": list(executor.findings),
            "counts": dict(executor.counts),
            "coverage": dict(executor.coverage),
            "evidence": list(report.evidence_frame_ids),
            "answers": list(report.state_answers),
            "alerts": list(report.alerts),
        }
        if self._model_split:
            report_input["model_runtime"] = dict(self._model_runtime)
        health = getattr(self, "health", None)
        if health is not None:
            state = health.state
            report_input["health"] = {
                "slow_unavailable": not state.slow_available,
                "slow_unavailable_since_s": state.unavailable_since,
                "slow_unavailable_reason": self._slow_unavailable_reason,
                "consecutive_slow_failures": state.consecutive_slow_failures,
                "slow_latencies_s": list(state.slow_latencies),
                "planning_failures": state.planning_failures,
                "resource_pressure": state.pressure_active,
                "low_disk": state.low_disk,
                "active_reasons": sorted(state.active_reasons),
            }
        summary, chinese, english = summarize_mission(
            report_input,
            executor.plan,
            self.task_text,
        )
        # The collector uses <out>/<episode-id>-onboard-frames as work_dir.
        suffix = "-onboard-frames"
        directory = (
            self.work_dir.parent / "episodes" / self.work_dir.name.removesuffix(suffix)
            if self.work_dir.name.endswith(suffix)
            else self.work_dir
        )
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "mission_summary.json").write_text(summary_json(summary), encoding="utf-8")
        (directory / "mission_summary.txt").write_text(
            chinese + "\n\n" + english + "\n", encoding="utf-8"
        )

    def _slow_identity(
        self, frame_path: Path, current, focus, task_text: str | None = None
    ) -> dict[str, Any]:
        """Ask the slow system about ``focus``; candidates are renamed track-N as trained."""
        shown = [focus] + [t for t in current if t.track_id != focus.track_id]
        shown = shown[:MAX_PROMPT_CANDIDATES]
        width, height = self.image_size
        names = {t.track_id: f"track-{index + 1}" for index, t in enumerate(shown)}
        row = {
            "image_path": str(frame_path),
            "task_text": task_text or self.task_text,
            "memory": MEMORY,
            "candidates": [
                {
                    "track_id": names[t.track_id],
                    "box_xyxy_normalized": [
                        round(min(1.0, max(0.0, t.bbox[0] / width)), 4),
                        round(min(1.0, max(0.0, t.bbox[1] / height)), 4),
                        round(min(1.0, max(0.0, t.bbox[2] / width)), 4),
                        round(min(1.0, max(0.0, t.bbox[3] / height)), 4),
                    ],
                }
                for t in shown
            ],
            "focus_track_id": names[focus.track_id],
            "_crop_policy": "candidate_zoom_v2",
        }
        from scripts.eval_slow_system import decision_confidence, parse_model_decision

        if getattr(self, "health", None) is not None and self.health.state.low_disk:
            self._slow_skipped_current = True
            return {
                "track_id": focus.track_id,
                "identity": None,
                "error": "slow_call_skipped_low_disk",
                "latency_s": 0.0,
            }

        started = time.perf_counter()
        try:
            lock = getattr(self, "_model_lock", None)
            shutdown = getattr(self, "_model_shutdown", None)

            def call_once():
                restart_thread = getattr(self, "_slow_restart_thread", None)
                if restart_thread is not None and not restart_thread.is_alive():
                    restart_thread.join(timeout=0)
                    context = lock if lock is not None else nullcontext()
                    with context:
                        self._slow_restart_thread = None
                        if self._slow_restart_result is not None:
                            self.slow = self._slow_restart_result
                            self._slow_restart_result = None
                            self.health.runner = self.slow
                        elif self._slow_restart_error is not None:
                            self._slow_restart_error = None
                            self._slow_restart_next_retry = time.monotonic() + self.slow_restart_interval_s
                context = lock if lock is not None else nullcontext()
                with context:
                    if shutdown is not None and shutdown.is_set():
                        raise RuntimeError("onboard policy is shutting down")
                    runner = self.slow
                    if runner is None:
                        raise ConnectionRefusedError("慢模型不可用: restart pending")
                    return runner(row)

            def restart_runner():
                context = lock if lock is not None else nullcontext()
                with context:
                    if self._model_switching:
                        raise RuntimeError("慢模型正在切换，跳过身份重建")
                    old_runner = self.slow
                    self.slow = None
                    self._slow_restart_attempts += 1
                    generation = self._slow_restart_generation

                def build_runner():
                    try:
                        close = getattr(old_runner, "close", None)
                        if callable(close):
                            try:
                                close(wait_port_free=False)
                            except TypeError:
                                close()
                        runner = self._make_identity_runner()
                        runner.request_timeout_s = self.slow_call_timeout_s
                        runner.max_restarts = 0
                        stale = (
                            (shutdown is not None and shutdown.is_set())
                            or generation != self._slow_restart_generation
                        )
                        if stale:
                            close = getattr(runner, "close", None)
                            if callable(close):
                                close()
                            return
                    except Exception as error:
                        self._slow_restart_error = error
                    else:
                        with context:
                            stale = (
                                (shutdown is not None and shutdown.is_set())
                                or generation != self._slow_restart_generation
                            )
                            if stale:
                                close = getattr(runner, "close", None)
                                if callable(close):
                                    close()
                            else:
                                self._slow_restart_result = runner

                self._slow_restart_result = None
                self._slow_restart_error = None
                self._slow_restart_thread = threading.Thread(
                    target=build_runner, name="onboard-slow-restart", daemon=True
                )
                self._slow_restart_thread.start()
                health = getattr(self, "health", None)
                if health is not None:
                    health.runner = None

            def service_is_gone(error: Exception) -> bool:
                runner = getattr(self, "slow", None)
                process = getattr(runner, "process", None) if runner is not None else None
                if process is not None and process.poll() is not None:
                    return True
                if isinstance(error, ConnectionRefusedError):
                    return True
                reason = getattr(error, "reason", None)
                if isinstance(reason, ConnectionRefusedError):
                    return True
                return getattr(error, "errno", None) in {10061, 111}

            try:
                raw, latency = call_once()
            except Exception as exc:
                budget = float(getattr(self, "slow_restart_budget_s", 0.0) or 0.0)
                gone = service_is_gone(exc)
                restart_limit = getattr(self, "slow_restart_limit", 1)
                next_retry = getattr(self, "_slow_restart_next_retry", 0.0)
                if (
                    not gone
                    or budget <= 0
                    or self._slow_restart_attempts >= restart_limit
                    or time.monotonic() < next_retry
                ):
                    raise
                deadline = time.monotonic() + budget
                last_error: Exception = exc
                recovered = False
                while time.monotonic() < deadline and self._slow_restart_attempts < restart_limit:
                    try:
                        if lock is not None:
                            with lock:
                                restart_runner()
                        else:
                            restart_runner()
                    except Exception as restart_error:
                        last_error = restart_error
                        self.slow = None
                        if self._slow_restart_thread is None:
                            self._slow_restart_next_retry = time.monotonic() + self.slow_restart_interval_s
                        if self.health is not None:
                            self.health.runner = None
                    else:
                        try:
                            raw, latency = call_once()
                        except Exception as retry_error:
                            last_error = retry_error
                        else:
                            recovered = True
                            break
                    time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
                if not recovered:
                    raise last_error from exc
        except Exception as exc:  # timeouts or server faults leave the track unverified
            self._slow_failed_current = True
            self._slow_unavailable_reason = f"慢模型不可用: {type(exc).__name__}: {exc}"
            health = getattr(self, "health", None)
            if health is not None:
                health.record_slow_call(time.perf_counter() - started, ok=False)
            supervisor = getattr(self, "supervisor", None)
            record_unavailable = getattr(supervisor, "record_slow_model_unavailable", None)
            if callable(record_unavailable):
                record_unavailable(type(exc).__name__, str(exc))
            return {
                "track_id": focus.track_id,
                "identity": None,
                "error": f"{type(exc).__name__}: {exc}",
                "latency_s": time.perf_counter() - started,
            }
        self.slow_calls += 1
        decision, error = parse_model_decision(raw)
        health = getattr(self, "health", None)
        if health is not None:
            health.record_slow_call(float(latency), ok=decision is not None)
        if decision is None:
            self._slow_failed_current = True
            self._slow_unavailable_reason = f"慢模型不可用: invalid output: {error}"
            supervisor = getattr(self, "supervisor", None)
            record_unavailable = getattr(supervisor, "record_slow_model_unavailable", None)
            if callable(record_unavailable):
                record_unavailable("invalid_output", str(error))
        identity = identity_of(decision["decision"]) if decision else None
        return {
            "track_id": focus.track_id,
            "identity": identity,
            "decision": decision["decision"] if decision else None,
            "reason": decision["reason"] if decision else None,
            "confidence": decision_confidence(getattr(self.slow, "last_logprobs", None)),
            "error": error,
            "latency_s": float(latency),
        }

    def _run_near_depth(
        self,
        *,
        loop: OnboardLoop | None,
        current: Sequence[Any],
        rgb: Any,
        frame_id: str,
        pose_ned: tuple[float, float, float],
        camera_info: Mapping[str, Any],
        camera_ray: Callable[[float, float], Any],
        timestamp_s: float | None,
        stage_allowed: bool,
        step_token: int,
    ) -> None:
        """Run the optional depth source after all slow calls, synchronously."""
        if (
            not self.near_depth.enabled
            or loop is None
            or not current
            or rgb is None
            or camera_ray is None
        ):
            return
        if not all(key in camera_info for key in ("fx", "fy", "cx", "cy")):
            self.near_depth.stats.skip("calibration")
            return
        focus_id = getattr(loop, "_focus", None)
        focus = next((track for track in current if track.track_id == focus_id), None)
        if focus is None:
            return
        center = ((focus.bbox[0] + focus.bbox[2]) / 2.0, (focus.bbox[1] + focus.bbox[3]) / 2.0)
        ray_data = camera_ray(*center)
        if ray_data is None:
            current_range = None
        else:
            origin, _ = ray_data
            estimate = getattr(focus, "position_estimate", None)
            if estimate is not None:
                current_range = math.dist(tuple(origin), tuple(estimate.position_ned))
            elif focus.ground is not None:
                current_range = math.sqrt(
                    (focus.ground[0] - pose_ned[0]) ** 2
                    + (focus.ground[1] - pose_ned[1]) ** 2
                    + pose_ned[2] ** 2
                )
            else:
                current_range = None
        from multidetect.multimodal_ranging import CameraCalibration

        calibration = CameraCalibration(
            calibration_id="onboard-policy",
            width_px=self.image_size[0],
            height_px=self.image_size[1],
            fx_px=float(camera_info["fx"]),
            fy_px=float(camera_info["fy"]),
            cx_px=float(camera_info["cx"]),
            cy_px=float(camera_info["cy"]),
        )
        self.near_depth.run(
            track=focus,
            image_bgr=rgb[:, :, ::-1].copy(),
            frame_id=frame_id,
            captured_at_s=float(timestamp_s if timestamp_s is not None else time.monotonic()),
            now_s=float(timestamp_s if timestamp_s is not None else time.monotonic()),
            camera_ray=camera_ray,
            current_range_m=current_range,
            calibration=calibration,
            image_size=self.image_size,
            stage_allowed=stage_allowed,
            focus=True,
            step_token=step_token,
        )

    def _sync_vehicle_mode(self, observation: dict[str, Any]) -> None:
        if getattr(self, "motion_adapter", None) is None:
            return
        # SIM observations carry EXTENDED_SYS_STATE.vtol_state (3 MC, 4 FW) under
        # flight_state; the real runtime passes a mode string.
        flight_state = observation.get("flight_state") or {}
        telemetry_mode = observation.get("vtol_state")
        if telemetry_mode is None and isinstance(flight_state, dict):
            telemetry_mode = flight_state.get("vtol_state")
        if telemetry_mode is None:
            telemetry_mode = observation.get("mode")
        if telemetry_mode is not None:
            self.motion_adapter.set_mode(telemetry_mode)

    def _set_active_camera_mode(self, mode: str) -> None:
        """Keep the loop's scalar camera model aligned with the active VTOL mode."""
        if not getattr(self, "_hybrid_vtol", False):
            return
        pitch = profile_mount_pitch_down_rad(self.airframe_profile, 0.18, mode=mode.lower())
        self.camera_pitch_down_rad = pitch
        loop = self._hybrid_active_loop()
        if loop is None:
            return
        loop.camera = CameraModel(
            fx=loop.camera.fx,
            fy=loop.camera.fy,
            cx=loop.camera.cx,
            cy=loop.camera.cy,
            width=loop.camera.width,
            height=loop.camera.height,
            pitch_down_rad=pitch,
        )

    def _hybrid_active_loop(self):
        executor = getattr(self, "executor", None)
        return (
            getattr(executor, "active_loop", None)
            if executor is not None
            else getattr(self, "loop", None)
        )

    @staticmethod
    def _hybrid_search_skill(step) -> bool:
        return step is None or getattr(step, "skill", None) in {
            "area_search",
            "count",
            "survey",
            "patrol",
            "monitor",
        }

    def _hybrid_sync_step(self) -> None:
        executor = getattr(self, "executor", None)
        if executor is None:
            return
        step = getattr(executor, "current_step", None)
        token = getattr(step, "id", None) if step is not None else None
        if token == getattr(self, "_hybrid_step_token", None):
            return
        self._hybrid_step_token = token
        self._hybrid_mc_engaged = False
        self._hybrid_mc_search_steps = 0
        self._hybrid_rejections_seen = 0
        self._hybrid_transition_count = 0
        self._hybrid_candidate_visible = False
        self._hybrid_fw_reentry_backoff_until = -1

    def _active_search(self, fallback, mc_fallback=None):
        if not self._hybrid_vtol:
            return fallback
        loop = self._hybrid_active_loop()
        if loop is not None and self.motion_adapter.mode == "FW" and loop.state == "searching":
            return fallback
        return mc_fallback or self._search_mc

    def _hybrid_candidate_blocks_reentry(self, current) -> bool:
        """Whether a live track can still justify staying in MC."""
        loop = self._hybrid_active_loop()
        if loop is None:
            return False
        eligible = {track.track_id for track in loop.tracks_needing_verdict(current)}
        max_age = loop.config.vtol_fw_reentry_candidate_max_steps
        for track in current:
            if track.exhausted:
                continue
            if track.verdict in {"approach", "pending_near", "confirm"}:
                return True
            if (
                track.verdict is None
                and track.track_id in eligible
                and loop.step_index - track.first_step <= max_age
            ):
                return True
        return False

    def _energy_vehicle_estimator(
        self, observation: Mapping[str, Any], samples: Sequence[Mapping[str, Any]]
    ):
        """Return the energy model when SIM telemetry is judged on the vehicle clock.

        The collector stamps each sample with PX4's boot clock at receipt.  In
        SITL that is the lockstep simulation clock, which on a loaded host runs
        at a fraction of host time; host-clock ages then grow with primitive and
        inference wall time and a fresh battery reading looks stale.  Measuring
        both receipt and "now" on the vehicle clock gives each sample its true
        age.  Flight keeps host receipt time: there, a stall really ages data.
        """
        estimator = getattr(self.supervisor, "energy_estimator", None)
        if estimator is None or getattr(self.supervisor, "_flight_mode", False):
            return None
        stamps = [_vehicle_receipt_s(sample) for sample in samples]
        stamps = [stamp for stamp in stamps if stamp is not None]
        if not estimator.clock_bound and not stamps:
            return None
        boot_ms = observation.get("pose_time_boot_ms")
        if isinstance(boot_ms, (int, float)) and not isinstance(boot_ms, bool):
            if math.isfinite(boot_ms) and boot_ms >= 0:
                stamps.append(boot_ms / 1000.0)
        previous = getattr(self, "_energy_vehicle_now_s", None)
        self._energy_vehicle_now_s = max(stamps + ([previous] if previous is not None else []))
        if not estimator.clock_bound and not estimator.use_clock(
            lambda: self._energy_vehicle_now_s
        ):
            return None
        return estimator

    def _note_vehicle_telemetry_gaps(self, estimator, observation: Mapping[str, Any]) -> None:
        """Hand the reader's no-telemetry spans (vehicle clock) to the energy model.

        Run9: the host process stalled for 20 s, 7 s of PX4 telemetry were lost,
        and the next battery report was due only after a fresh pose arrived, so
        the battery alone looked 7.7 s old and the mission returned.  The gap
        is logged as evidence; the battery is then judged from link recovery.
        """
        for gap in observation.get("vehicle_telemetry_gaps_s") or []:
            if not isinstance(gap, (list, tuple)) or len(gap) != 2:
                continue
            start, end = gap
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in gap):
                continue
            if estimator.note_delivery_gap(float(start), float(end)):
                self.supervisor.log.append(
                    "vehicle_telemetry_gap",
                    self.supervisor.clock(),
                    vehicle_start_s=float(start),
                    vehicle_end_s=float(end),
                    gap_s=float(end) - float(start),
                )

    def _act_impl(self, observation: dict[str, Any], *, pose_ned, yaw_deg: float):
        # Replace on every frame, including missing feedback: a gimbal search
        # must never count coverage using the previous frame's camera pose.
        self._search_camera_pose = observation.get("camera_pose")
        self._sync_vehicle_mode(observation)
        if getattr(self, "motion_adapter", None) is not None:
            self._set_active_camera_mode(self.motion_adapter.mode)
        self._slow_failed_current = False
        self._slow_skipped_current = False
        # Process external safety inputs first. A latched supervisor decision and
        # one-shot ground event must never be hidden by a health hold.
        if self.supervisor is not None:
            if self.sim_timeline is not None:
                self.sim_timeline.advance(observation, self.supervisor, pose_ned=pose_ned)
            samples = observation.get("energy_telemetry") or []
            vehicle_estimator = self._energy_vehicle_estimator(observation, samples)
            if samples and vehicle_estimator is None:
                # Clamp host receipts against the current clock, not the last
                # evaluation's: a stale ``_now`` would backdate fresh samples.
                self.supervisor._time()
            for sample in samples:
                if vehicle_estimator is not None:
                    # One domain: receipt and "now" are both on the vehicle clock.
                    received_at = _vehicle_receipt_s(sample)
                    if received_at is not None:
                        vehicle_estimator.ingest(sample["message"], received_at_s=received_at)
                    continue
                received_at = float(sample["received_monotonic_s"])
                received_at += getattr(self, "_energy_receipt_clock_offset", 0.0)
                received_at = min(received_at, self.supervisor._now)
                self.supervisor.ingest_energy_telemetry(
                    sample["message"], received_at_s=received_at
                )
            if vehicle_estimator is not None:
                self._note_vehicle_telemetry_gaps(vehicle_estimator, observation)
            for event in observation.get("ground_events", []):
                self.supervisor.ground_event(event)
            update_estimates = getattr(self.supervisor, "update_estimates", None)
            if update_estimates is not None:
                update_estimates(**observation.get("mission_estimates", {}))
            evaluate = getattr(self.supervisor, "evaluate", None)
            if evaluate is not None and evaluate(pose_ned=pose_ned) != "continue":
                action, info = self.supervisor.next_action(
                    pose_ned=tuple(pose_ned),
                    yaw_deg=yaw_deg,
                    landed=observation.get("landed"),
                    timestamp_s=_observation_timestamp_s(observation),
                )
                supervised_labels = {
                    **info,
                    "state": "supervised",
                    "detections": 0,
                    "tracks": [],
                    "slow_calls": [],
                    "confirmed": None,
                    "alert_vote_clusters": [],
                    "fast_latency_s": 0.0,
                    "mission": {"history": list(self.supervisor.history)},
                }
                if getattr(getattr(self, "health", None), "config", None) is not None and getattr(
                    self.health.config, "enabled", False
                ):
                    supervised_labels["health"] = {}
                return action, {"onboard": supervised_labels}
        if getattr(getattr(self, "supervisor", None), "planned_model_switch", False):
            return ActionPrimitive.hold(1.0), {"onboard": {"state": "planned_model_switch", "detections": 0, "tracks": [], "slow_calls": []}}
        health_monitor = getattr(self, "health", None)
        health_decision = (
            health_monitor.on_frame(mission_requires_images=bool(self.executor is not None))
            if health_monitor is not None and not getattr(getattr(self, "supervisor", None), "planned_model_switch", False)
            else HealthDecision()
        )
        # A completed recovery must not be reused by a later degradation
        # cycle.  Likewise, once health is back to ``ok`` and the main
        # mission resumes, discard any stale recovery executor so the next
        # escalation gets a fresh plan and state.
        if (
            health_decision.decision == "ok"
            and getattr(self, "_health_recovery_executor", None) is not None
        ):
            self._health_recovery_executor = None
        if health_decision.decision in {"return_home", "land"}:
            if self.supervisor is not None:
                self.supervisor._escalate(
                    "land_now" if health_decision.decision == "land" else "return_now",
                    health_decision.reason or "health degradation",
                )
                action, info = self.supervisor.next_action(
                    pose_ned=tuple(pose_ned), yaw_deg=yaw_deg
                )
                return action, {"onboard": {**info, "health": health_decision.__dict__}}
            if self.executor is not None:
                action, info = self._health_recovery_action(
                    pose_ned=pose_ned,
                    yaw_deg=yaw_deg,
                    land=health_decision.decision == "land",
                    timestamp_s=_observation_timestamp_s(observation),
                )
                return action, {"onboard": {**info, "health": health_decision.__dict__}}
            return ActionPrimitive.hold(1.0), {
                "onboard": {"state": health_decision.decision, "health": health_decision.__dict__}
            }
        if health_decision.decision == "hold":
            return ActionPrimitive.hold(1.0), {
                "onboard": {"state": "health_hold", "health": health_decision.__dict__}
            }
        if health_decision.detector_skip:
            return ActionPrimitive.hold(1.0), {
                "onboard": {"state": "health_hold", "health": health_decision.__dict__}
            }
        import numpy as np
        from PIL import Image

        step = self._step
        self._step += 1
        rgb = np.asarray(Image.open(io.BytesIO(observation["rgb"])).convert("RGB"))
        suffix = "jpg" if self.frame_format == "jpeg" else "png"
        frame_path = self.work_dir / f"frame_{step:04d}.{suffix}"
        if health_decision.stop_frame_writes:
            # Low disk: write no frame; slow calls are skipped by _slow_identity.
            pass

        elif self.frame_format == "jpeg":
            Image.fromarray(rgb, mode="RGB").save(
                frame_path, format="JPEG", quality=self.frame_quality, optimize=True
            )
        else:
            frame_path.write_bytes(observation["rgb"])
        started = time.perf_counter()
        detections = self.proposer(rgb[:, :, ::-1].copy())
        fast_s = time.perf_counter() - started
        camera_info = observation["camera_info"]
        ground_model = self.ground_projection.for_observation(observation)

        def ground_point(u: float, v: float):
            projector = getattr(self, "ground_projector", None)
            if projector is not None:
                return projector(u, v, observation)
            # Full attitude + camera mount inverse of the projection model.
            if self.airframe_profile.camera.attitude_rotations:
                ray_data = camera_ray(u, v)
                if ray_data is None:
                    return None
                origin, direction = ray_data
                # camera_ray's origin is N/E/down; ground models (and the flat
                # ground) intersect from N/E/up.  Passing the down value made every
                # tailsitter ground point None in SITL (a confirmation without a
                # position).
                origin_neu = (origin[0], origin[1], -origin[2])
                if ground_model is not None:
                    return ground_model.intersect_ray(origin_neu, direction)
                if direction[2] <= 1e-6 or origin_neu[2] <= 0:
                    return None
                scale = origin_neu[2] / direction[2]
                return origin[0] + direction[0] * scale, origin[1] + direction[1] * scale
            return pixel_ground_point(
                u,
                v,
                pose=observation,
                camera_info=camera_info,
                image_size=self.image_size,
                ground_model=ground_model,
                camera_pitch_rad=self.camera_pitch_down_rad,
            )

        def camera_ray(u: float, v: float):
            projector = getattr(self, "ground_projector", None)
            if projector is not None:
                # An injected custom projector must expose its own camera
                # model; silently assuming the simulator model would mix rays.
                ray = getattr(projector, "ray", None)
                return ray(u, v, observation) if callable(ray) else None
            base = pixel_camera_ray(
                u,
                v,
                pose=observation,
                camera_info=camera_info,
                image_size=self.image_size,
                camera_pitch_rad=self.camera_pitch_down_rad,
            )
            if base is None or not self.airframe_profile.camera.attitude_rotations:
                return base
            direction = camera_ray_from_profile(
                u,
                v,
                fx=float(camera_info["fx"]),
                fy=float(camera_info["fy"]),
                cx=float(camera_info["cx"]),
                cy=float(camera_info["cy"]),
                attitude_deg=(
                    float(observation["attitude"]["roll_deg"]),
                    float(observation["attitude"]["pitch_deg"]),
                    float(observation["attitude"]["yaw_deg"]),
                ),
                profile=self.airframe_profile,
                mode=self.motion_adapter.mode,
            )
            return base[0], direction

        ranging_enabled = getattr(
            getattr(getattr(self, "loop", None), "config", None),
            "step_triangulation",
            False,
        )
        ranging_enabled = ranging_enabled or getattr(
            getattr(self, "near_depth", None), "enabled", False
        )
        ranging_kwargs = (
            {"camera_ray": camera_ray, "timestamp_s": _observation_timestamp_s(observation)}
            if ranging_enabled
            else {}
        )

        if self.executor is not None:
            result = self._act_mission(
                frame_path,
                detections,
                fast_s,
                frame_rgb=rgb,
                pose_ned=pose_ned,
                yaw_deg=yaw_deg,
                ground_point=ground_point,
                landed=observation.get("landed"),
                timestamp_s=_observation_timestamp_s(observation),
                camera_info=camera_info,
                **({"camera_ray": camera_ray} if ranging_enabled else {}),
            )
            if self._slow_failed_current:
                labels = {"state": "health_hold"}
                if self._health_labels_enabled:
                    labels["health"] = {"decision": "hold", "reason": "slow_call_failed"}
                return ActionPrimitive.hold(1.0), {"onboard": labels}
            if self._slow_skipped_current:
                labels = {"state": "health_hold"}
                if self._health_labels_enabled:
                    labels["health"] = {"reason": "low_disk"}
                return ActionPrimitive.hold(1.0), {"onboard": labels}
            return result
        current = self.loop.update_tracks(
            detections,
            pose_ned=tuple(pose_ned),
            yaw_deg=yaw_deg,
            ground_point=ground_point,
            frame_rgb=rgb,
            **ranging_kwargs,
        )
        rejection_count_before = len(getattr(self.loop, "_rejections", ()))
        slow = []
        if self.loop.state in {"searching", "approaching"}:
            for track in self.loop.tracks_needing_verdict(current):
                verdict = self.loop.remembered_verdict(track.track_id)
                if verdict is None:
                    verdict = self._slow_identity(frame_path, current, track)
                if verdict.get("error"):
                    self._slow_failed_current = True
                    record_error = getattr(self.supervisor, "record_perception_error", None)
                    if callable(record_error):
                        record_error(track.track_id, verdict["error"])
                    slow.append(verdict)
                    break
                self.loop.apply_verdict(
                    track.track_id,
                    verdict.get("decision"),
                    verdict.get("confidence"),
                    remembered=verdict.get("remembered", False),
                )
                slow.append(verdict)
        new_rejection = len(getattr(self.loop, "_rejections", ())) > rejection_count_before
        self._hybrid_candidate_visible = self._hybrid_candidate_blocks_reentry(current)
        if (
            new_rejection
            and self.loop.state == "searching"
            and getattr(self.loop, "_focus", None) is None
        ):
            self._hybrid_rejections_seen = len(getattr(self.loop, "_rejections", ()))
            self._hybrid_mc_search_steps = 0
            self._hybrid_mc_engaged = False
        run_near_depth = getattr(self, "_run_near_depth", None)
        if callable(run_near_depth):
            run_near_depth(
                loop=self.loop,
                current=current,
                rgb=rgb,
                frame_id=frame_path.name,
                pose_ned=tuple(pose_ned),
                camera_info=camera_info,
                camera_ray=camera_ray,
                timestamp_s=_observation_timestamp_s(observation),
                stage_allowed=(
                    self.loop.state == "approaching" or getattr(self.loop, "_at_closest", False)
                ),
                step_token=step,
            )
        action, info = self.loop.next_action(
            current, pose_ned=tuple(pose_ned), yaw_deg=yaw_deg, ground_point=ground_point
        )
        if (
            self._hybrid_vtol
            and self.motion_adapter.mode == "MC"
            and self.loop.state == "searching"
            and getattr(self.loop, "_focus", None) is None
            and not self._hybrid_candidate_visible
            and getattr(self.loop, "_revisit", None) is None
            and not self.loop.unresolved_positive_tracks()
        ):
            self._hybrid_mc_search_steps += 1
        labels = {
            "onboard": {
                **info,
                "state": self.loop.state,
                "detections": len(detections),
                "tracks": [
                    {
                        "id": t.track_id,
                        "bbox": [round(v, 1) for v in t.bbox],
                        "verdict": t.verdict,
                        "verdict_remembered": getattr(t, "verdict_remembered", False),
                        "ground_ne": list(t.ground) if t.ground is not None else None,
                        "identity_positive_matches": getattr(t, "identity_positive_matches", 0),
                        "identity_reject_matches": getattr(t, "identity_reject_matches", 0),
                        "identity_verdict_history": list(
                            getattr(t, "identity_verdict_history", ())
                        ),
                    }
                    for t in current
                ],
                "slow_calls": slow,
                "alert_vote_clusters": [],
                "fast_latency_s": round(fast_s, 4),
                "confirmed": self.loop.confirmed,
            }
        }
        if getattr(getattr(self, "near_depth", None), "enabled", False):
            labels["onboard"]["near_depth"] = self.near_depth.stats.as_dict()
        if not math.isfinite(sum(pose_ned)):
            raise RuntimeError("non-finite pose")
        if self._slow_failed_current:
            labels = {"state": "health_hold"}
            if self.health.config.enabled:
                labels["health"] = {"decision": "hold", "reason": "slow_call_failed"}
            return ActionPrimitive.hold(1.0), {"onboard": labels}
        if self._slow_skipped_current:
            labels = {"state": "health_hold"}
            if self.health.config.enabled:
                labels["health"] = {"reason": "low_disk"}
            return ActionPrimitive.hold(1.0), {"onboard": labels}
        return action, labels

    def act(self, observation: dict[str, Any], *, pose_ned, yaw_deg: float):
        """Single return point: every policy primitive crosses the envelope."""
        supervisor = getattr(self, "supervisor", None)
        if supervisor is not None:
            # Preserve the PX4 boot clock in every checkpoint emitted by the
            # supervisor.  Observation fields are sourced from MAVLink state.
            flight_state = observation.get("flight_state")
            boot_time_ms = observation.get("time_boot_ms")
            if boot_time_ms is None and isinstance(flight_state, dict):
                boot_time_ms = flight_state.get("time_boot_ms")
            if isinstance(boot_time_ms, (int, float)) and not isinstance(boot_time_ms, bool):
                supervisor.boot_time_ms = boot_time_ms
        if getattr(self, "operator_link_waiting", False):
            return ActionPrimitive.hold(1.0), {
                "onboard": {"state": "operator_link_waiting", "mission": "operator_link"}
            }
        self._sync_vehicle_mode(observation)
        self._hybrid_sync_step()
        adapter = getattr(self, "motion_adapter", None)
        hybrid_loop = self._hybrid_active_loop()
        hybrid_search = self._hybrid_search_skill(
            getattr(getattr(self, "executor", None), "current_step", None)
        )
        if adapter is not None and self._hybrid_vtol:
            recovering_after_rejection = bool(
                adapter.mode == "MC"
                and hybrid_search
                and self._hybrid_rejections_seen
                and hybrid_loop is not None
                and self._hybrid_transition_count < hybrid_loop.config.vtol_fw_transition_budget
            )
            adapter.preferred_mode = (
                "MC"
                if self._hybrid_mc_engaged
                or not hybrid_search
                or hybrid_loop is None
                or hybrid_loop.state != "searching"
                or recovering_after_rejection
                or (
                    adapter.mode == "MC"
                    and self._hybrid_transition_count
                    >= hybrid_loop.config.vtol_fw_transition_budget
                )
                else "FW"
            )
            self._set_active_camera_mode(adapter.mode)
            if adapter.transitioning:
                pending = adapter.from_primitive(
                    ActionPrimitive.hold(1.0), pose_ned=pose_ned, yaw_deg=yaw_deg
                )
                pending, safety_info = adapter.guard_command(
                    pending, pose_ned=pose_ned, yaw_deg=yaw_deg
                )
                return pending, {
                    "onboard": {"state": "transition", "transition_pending": True},
                    "motion": pending.to_dict()
                    if isinstance(pending, MotionCommand)
                    else {"kind": pending.kind},
                    "airframe": adapter.profile.id,
                    **({"safety": safety_info} if safety_info else {}),
                }
        transition = adapter.mode_change_command() if adapter is not None else None
        if transition is not None:
            # A hover-mode VTOL reported in FW (e.g. right after a VTOL takeoff and
            # transition): convert first; the loop's next step runs in MC.
            command, safety_info = adapter.guard_command(
                transition, pose_ned=pose_ned, yaw_deg=yaw_deg
            )
            onboard = {
                "state": "transition",
                "detections": 0,
                "tracks": [],
                "slow_calls": [],
                "confirmed": getattr(getattr(self, "loop", None), "confirmed", None),
                "vtol_mode": adapter.mode,
            }
            labels = {
                "onboard": onboard,
                "motion": command.to_dict(),
                "airframe": adapter.profile.id,
            }
            if safety_info:
                labels["safety"] = safety_info
            return command, labels
        action, labels = self._act_impl(observation, pose_ned=pose_ned, yaw_deg=yaw_deg)
        if (
            adapter is not None
            and self._hybrid_vtol
            and hybrid_search
            and hybrid_loop is not None
            and adapter.mode == "FW"
            and hybrid_loop.state == "approaching"
        ):
            self._hybrid_mc_engaged = True
            adapter.preferred_mode = "MC"
            transition = adapter.mode_change_command()
            if transition is not None:
                command, safety_info = adapter.guard_command(
                    transition, pose_ned=pose_ned, yaw_deg=yaw_deg
                )
                labels = {
                    **labels,
                    "onboard": {**labels.get("onboard", {}), "state": "transition"},
                    "motion": command.to_dict(),
                    "airframe": adapter.profile.id,
                }
                if safety_info:
                    labels["safety"] = safety_info
                return command, labels
        if (
            adapter is not None
            and self._hybrid_vtol
            and hybrid_search
            and hybrid_loop is not None
            and adapter.mode == "MC"
            and hybrid_loop.state == "searching"
            and getattr(hybrid_loop, "_focus", None) is None
            and self._hybrid_rejections_seen
            and self._hybrid_mc_search_steps >= hybrid_loop.config.vtol_fw_reentry_min_mc_steps
            and not self._hybrid_candidate_visible
            and getattr(hybrid_loop, "_revisit", None) is None
            and not hybrid_loop.unresolved_positive_tracks()
            and self._hybrid_transition_count < hybrid_loop.config.vtol_fw_transition_budget
            and hybrid_loop.step_index >= self._hybrid_fw_reentry_backoff_until
        ):
            adapter.preferred_mode = "FW"
            transition = adapter.mode_change_command()
            if transition is not None:
                command, safety_info = adapter.guard_command(
                    transition, pose_ned=pose_ned, yaw_deg=yaw_deg
                )
                if isinstance(command, MotionCommand) and command.kind == "transition":
                    self._hybrid_transition_count += 1
                    self._hybrid_mc_search_steps = 0
                elif safety_info.get("rejected") and "transition_altitude" in safety_info.get(
                    "reasons", ()
                ):
                    self._hybrid_fw_reentry_backoff_until = hybrid_loop.step_index + 3
                labels = {
                    **labels,
                    "onboard": {**labels.get("onboard", {}), "state": "transition"},
                    "motion": command.to_dict(),
                    "airframe": self.airframe_profile.id,
                }
                if safety_info:
                    labels["safety"] = safety_info
                return command, labels
        # Some lightweight test/integration adapters use ``None`` to indicate
        # that no motion was requested this frame; there is no outgoing
        # primitive to arbitrate in that case.
        if action is None:
            return action, labels
        envelope = getattr(self, "safety_envelope", None)
        # The legacy multirotor path below is intentionally byte-compatible.
        # Other airframes consume a command object; their executor must never
        # see a zero-speed hold or a zero-radius turn.
        if (
            getattr(self, "motion_adapter", None) is not None
            and not self.motion_adapter.is_multirotor
        ):
            command = self.motion_adapter.from_primitive(
                action, pose_ned=pose_ned, yaw_deg=yaw_deg, metadata=labels
            )
            if isinstance(command, MotionCommand):
                onboard = labels.get("onboard", {}) if isinstance(labels, dict) else {}
                phase = onboard.get("phase") if isinstance(onboard, dict) else None
                skill = onboard.get("state") if isinstance(onboard, dict) else None
                agl = observation.get("ground_agl", observation.get("agl_m"))
                if isinstance(agl, Mapping):
                    agl = agl.get("height_m", agl.get("agl_m"))
                try:
                    agl_value = None if agl is None else float(agl)
                except (TypeError, ValueError):
                    agl_value = None
                command, safety_info = self.motion_adapter.guard_command(
                    command,
                    pose_ned=pose_ned,
                    yaw_deg=yaw_deg,
                    current_agl_m=agl_value,
                    phase=phase,
                    skill=skill,
                )
                if safety_info:
                    labels = {**labels, "safety": safety_info}
                    if safety_info.get("rejected") and self.supervisor is not None:
                        self.supervisor.report_envelope_violation(
                            ", ".join(safety_info.get("reasons", ())) or "rejected motion command"
                        )
                labels = {
                    **labels,
                    "motion": command.to_dict(),
                    "airframe": self.airframe_profile.id,
                }
                return command, labels
        if envelope is None:
            return action, labels
        executor = getattr(self, "executor", None)
        step = getattr(executor, "current_step", None)
        phase = None
        if isinstance(labels, dict):
            onboard = labels.get("onboard", {})
            phase = onboard.get("phase") if isinstance(onboard, dict) else None
            if isinstance(onboard, dict) and onboard.get("supervisor_action") == "land_now":
                phase = "landing"
                # A terminal supervisor landing is an explicit landing skill;
                # never let the still-active mission step disable its bounded
                # low-altitude exception.
                step = type("_LandingStep", (), {"skill": "land"})()
        agl = observation.get("ground_agl", observation.get("agl_m"))
        if isinstance(agl, Mapping):
            agl = agl.get("height_m", agl.get("agl_m"))
        decision = envelope.apply(
            action,
            pose_ned=tuple(float(v) for v in pose_ned),
            yaw_deg=float(yaw_deg),
            current_agl_m=agl,
            phase=phase,
            skill=getattr(step, "skill", None),
        )
        if decision.rejected and self.supervisor is not None:
            self.supervisor.report_envelope_violation(
                ", ".join(decision.reasons) or "rejected primitive"
            )
        labels = {**labels, **decision.info()}
        return decision.action, labels

    def _health_recovery_action(self, *, pose_ned, yaw_deg: float, land: bool, timestamp_s=None):
        skill = "land" if land else "return_home"
        if (
            self._health_recovery_executor is None
            or self._health_recovery_executor.plan.steps[0].skill != skill
        ):
            plan = validate_mission_plan(
                {
                    "plan_version": 1,
                    "goal": "health recovery",
                    "report": ["status"],
                    "steps": [
                        {
                            "id": "recovery",
                            "skill": skill,
                            "params": {},
                            "on_success": None,
                            "on_fail": None,
                        }
                    ],
                }
            )
            recovery = MissionExecutor(plan, loop_factory=self.executor.loop_factory)
            recovery.home = getattr(self.executor, "home", None)
            recovery.cruise_altitude_m = getattr(self.executor, "cruise_altitude_m", None)
            self._health_recovery_executor = recovery
        action, info = self._health_recovery_executor.next_action(
            pose_ned=tuple(pose_ned), yaw_deg=yaw_deg, timestamp_s=timestamp_s
        )
        if self._health_recovery_executor.done:
            self._health_recovery_executor = None
        return action, info

    def _act_mission(
        self,
        frame_path,
        detections,
        fast_s,
        *,
        pose_ned,
        yaw_deg,
        ground_point,
        frame_rgb=None,
        landed=None,
        timestamp_s=None,
        camera_ray=None,
        camera_info=None,
    ):
        executor = self.executor
        step = executor.current_step
        perception_text = executor.current_task_text or self.task_text
        findings_before = len(executor.findings)
        mission_loop = getattr(executor, "active_loop", None)
        rejection_count_before = (
            len(getattr(mission_loop, "_rejections", ())) if mission_loop else 0
        )
        needing = executor.observe(
            detections,
            pose_ned=tuple(pose_ned),
            yaw_deg=yaw_deg,
            ground_point=ground_point,
            frame_rgb=frame_rgb,
            landed=landed,
            **(
                {"camera_ray": camera_ray, "timestamp_s": timestamp_s}
                if camera_ray is not None
                else {}
            ),
        )
        current = list(executor.current_tracks)
        slow = []
        for track in needing[:3]:
            loop = getattr(executor, "active_loop", None)
            # Inspection capture asks a distinct question from each deliberate
            # view, so those answers always come from the VLM.
            verdict = (
                loop.remembered_verdict(track.track_id)
                if loop is not None and step is not None and step.skill != "inspect"
                else None
            )
            if verdict is None:
                verdict = self._slow_identity(frame_path, current, track, perception_text)
            if verdict.get("remembered", False):
                executor.apply_verdict(track.track_id, verdict["decision"], remembered=True)
            else:
                if verdict.get("error"):
                    self._slow_failed_current = True
                    record_error = getattr(
                        getattr(self, "supervisor", None), "record_perception_error", None
                    )
                    if callable(record_error):
                        record_error(track.track_id, verdict["error"])
                    slow.append(verdict)
                    break
                executor.apply_verdict(
                    track.track_id, verdict.get("decision"), verdict.get("confidence")
                )
                record_error = getattr(
                    getattr(self, "supervisor", None), "record_perception_error", None
                )
                if verdict.get("decision") is None and callable(record_error):
                    record_error(track.track_id, verdict.get("error") or "no decision")
            slow.append(verdict)
        mission_loop = getattr(executor, "active_loop", None)
        new_rejection = len(getattr(mission_loop, "_rejections", ())) > rejection_count_before
        candidate_gate = getattr(self, "_hybrid_candidate_blocks_reentry", None)
        self._hybrid_candidate_visible = (
            candidate_gate(current) if callable(candidate_gate) else False
        )
        if (
            new_rejection
            and mission_loop is not None
            and mission_loop.state == "searching"
            and getattr(mission_loop, "_focus", None) is None
        ):
            self._hybrid_rejections_seen = len(getattr(mission_loop, "_rejections", ()))
            self._hybrid_mc_search_steps = 0
            self._hybrid_mc_engaged = False
        if (
            getattr(self, "_hybrid_vtol", False)
            and mission_loop is not None
            and self.motion_adapter.mode == "MC"
            and mission_loop.state == "searching"
            and getattr(mission_loop, "_focus", None) is None
            and not self._hybrid_candidate_visible
            and getattr(mission_loop, "_revisit", None) is None
            and not mission_loop.unresolved_positive_tracks()
        ):
            self._hybrid_mc_search_steps += 1
        run_near_depth = getattr(self, "_run_near_depth", None)
        if callable(run_near_depth):
            mission_run = getattr(executor, "_run", None)
            inspect_near_phase = (
                step is not None
                and step.skill == "inspect"
                and getattr(mission_run, "phase", None) in {"face", "capture"}
            )
            run_near_depth(
                loop=mission_loop,
                current=current,
                rgb=frame_rgb,
                frame_id=frame_path.name,
                pose_ned=tuple(pose_ned),
                camera_info=camera_info or {},
                camera_ray=camera_ray,
                timestamp_s=timestamp_s,
                stage_allowed=(
                    step is not None
                    and (
                        inspect_near_phase
                        or getattr(mission_loop, "state", None) == "approaching"
                        or getattr(mission_loop, "_at_closest", False)
                    )
                ),
                step_token=self._step,
            )
        alert_vote_clusters = list(getattr(executor, "alert_vote_clusters", ()))
        action, info = executor.next_action(
            pose_ned=tuple(pose_ned),
            yaw_deg=yaw_deg,
            ground_point=ground_point,
            frame_id=frame_path.name,
            landed=landed,
            timestamp_s=timestamp_s,
        )
        confirmed = None
        if len(executor.findings) > findings_before:
            finding = executor.findings[-1]
            confirmed = {
                "track_id": finding["track_id"],
                "bbox": finding["bbox"],
                "position_estimate_ne": list(finding["position_ne"]),
                "rule": finding["rule"],
            }
            confirmed.update(
                {
                    key: value
                    for key, value in finding.items()
                    if key.startswith("position_") and key != "position_ne"
                }
            )
        report = executor.report()
        labels = {
            "onboard": {
                **{k: v for k, v in info.items() if k not in {"counted", "confirmed"}},
                "state": executor.current_step.skill if executor.current_step else "done",
                "state_before": "approaching" if confirmed else (step.skill if step else "done"),
                "detections": len(detections),
                "tracks": [
                    {
                        "id": t.track_id,
                        "bbox": [round(v, 1) for v in t.bbox],
                        "verdict": t.verdict,
                        "verdict_remembered": getattr(t, "verdict_remembered", False),
                        "ground_ne": (
                            list(t.ground) if getattr(t, "ground", None) is not None else None
                        ),
                        "identity_positive_matches": getattr(t, "identity_positive_matches", 0),
                        "identity_reject_matches": getattr(t, "identity_reject_matches", 0),
                        "identity_verdict_history": list(
                            getattr(t, "identity_verdict_history", ())
                        ),
                    }
                    for t in current
                ],
                "slow_calls": slow,
                "alert_vote_clusters": alert_vote_clusters,
                "fast_latency_s": round(fast_s, 4),
                "confirmed": confirmed,
                "mission": {
                    "history": list(executor.history),
                    "evidence": list(report.evidence_frame_ids),
                    "answers": list(report.state_answers),
                    "alerts": list(report.alerts),
                    "positions": list(report.positions),
                    "counts": dict(executor.counts),
                    "alert_vote_clusters": alert_vote_clusters,
                },
            }
        }
        if self.mission_plan is not None and not self._mission_plan_labeled:
            labels["onboard"]["mission"]["plan"] = self._mission_plan_label()
            self._mission_plan_labeled = True
        if getattr(getattr(self, "near_depth", None), "enabled", False):
            labels["onboard"]["near_depth"] = self.near_depth.stats.as_dict()
        if not math.isfinite(sum(pose_ned)):
            raise RuntimeError("non-finite pose")
        return action, labels

    def _mission_plan_label(self) -> dict[str, Any]:
        plan = self.mission_plan
        report = self.mission_plan_report or {}
        return {
            "skills": [step.skill for step in plan.steps] if plan else [],
            "repairs": list(report.get("repairs", [])),
            "error": report.get("error"),
            "latency_s": round(sum(report.get("attempts", [])), 2),
        }


def build_onboard_policy(
    *,
    task_text: str,
    world_path: Path | None,
    altitude_m: float,
    camera_info: dict[str, float],
    image_size: tuple[int, int],
    work_dir: Path,
    config_path: Path | None = None,
    frame_format: str = "png",
    frame_quality: int = 90,
    airframe: str | None = None,
    runtime_step_limit: int | None = None,
) -> OnboardPolicy:
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path else None
    if airframe is not None:
        # The collector flies a specific airframe; the policy must plan for it.
        config = {**(config or {}), "airframe": airframe}
    if runtime_step_limit is not None:
        config = {**(config or {}), "runtime_step_limit": int(runtime_step_limit)}
    return OnboardPolicy(
        task_text=task_text,
        world_path=world_path,
        altitude_m=altitude_m,
        camera_info=camera_info,
        image_size=image_size,
        work_dir=work_dir,
        config=config,
        frame_format=frame_format,
        frame_quality=frame_quality,
    )


__all__ = [
    "OnboardPolicy",
    "VOCABULARY",
    "YoloeProposer",
    "build_mission_from_text",
    "build_onboard_policy",
    "labelled_refs",
    "perception_task_text",
]
