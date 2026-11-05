"""Bounded constructor assembly for the onboard policy facade."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from multidetect.autonomy.energy_estimator import EnergyConfig
from multidetect.autonomy.fw_search import FixedWingSearch, camera_footprint_width_m
from multidetect.autonomy.mission_executor import (
    ExecutorConfig,
    MissionExecutor,
    plan_for_count_task,
    plan_for_find_and_inspect,
    plan_for_find_task,
)
from multidetect.autonomy.mission_package import check_plan_against_package
from multidetect.autonomy.mission_plan import MissionPlan, PlanDecision
from multidetect.autonomy.mission_supervisor import MissionSupervisor
from multidetect.autonomy.motion_intent import profile_mount_pitch_down_rad
from multidetect.autonomy.airframe_profile import CameraMountProfile
from multidetect.autonomy.obstacle_source import CompositeSource, StaticMapSource
from multidetect.autonomy.onboard_loop import CameraModel, LoopConfig, OnboardLoop
from multidetect.autonomy.onboard_mission_log import DownlinkQueue, OnboardMissionLog
from multidetect.autonomy.onboard_policy_parts.helpers import labelled_refs
from multidetect.autonomy.plan_validation import validate_plan_for_execution
from multidetect.onboard_runtime.health import HealthConfig
from multidetect.sim_teacher.policies import (
    ExpertSearchPolicy,
    load_sdf_obstacles,
    segment_clear_of_obstacles,
)
from multidetect.simulator.task_semantics import typical_object_size


@dataclass(frozen=True)
class RuntimeRunnerState:
    """Startup-only state shared by runner and executor assembly."""

    split_planning: bool
    planner_start_error: Exception | None
    start_identity_runner: Callable[[], Any]


def build_runtime_loop_factory(
    self: Any,
    *,
    options: dict[str, Any],
    task_text: str,
    world_path: Path | None,
    altitude_m: float,
    camera_info: dict[str, float],
    image_size: tuple[int, int],
    home_ned: tuple[float, float, float] | None,
    hover_ops: bool,
    hybrid_vtol: bool,
    plans_like_multirotor: bool,
    near_depth_enabled: bool,
    position_defaults: dict[str, Any],
) -> Callable[..., OnboardLoop]:
    """Assemble the shared search state and the per-step loop factory."""
    # The x500 camera is pitched 0.18 rad down; other airframes carry their
    # own mount (the rc_cessna camera looks straight ahead).  Projecting a
    # fixed-wing frame with the x500 pitch puts every ground point ~10 deg
    # too steep, i.e. tens of metres short at 30 m altitude.
    # A hover-mode VTOL's loop sees the world through its MC (level) attitude.
    mc_mount_pitch = float(
        options.get(
            "camera_pitch_down_rad",
            0.18
            if self.motion_adapter.is_multirotor
            else profile_mount_pitch_down_rad(
                self.airframe_profile,
                0.18,
                mode="mc" if (hover_ops or hybrid_vtol) else None,
            ),
        )
    )
    fw_mount_pitch = (
        float(
            options.get(
                "camera_pitch_down_rad",
                profile_mount_pitch_down_rad(self.airframe_profile, 0.18, mode="fw"),
            )
        )
        if hybrid_vtol
        else mc_mount_pitch
    )
    mount_pitch = fw_mount_pitch if hybrid_vtol else mc_mount_pitch
    self.camera_pitch_down_rad = mount_pitch
    search_pattern = str(options.get("search_pattern", "current"))
    if search_pattern not in {"current", "lawnmower", "spiral", "two_altitude"}:
        raise ValueError(
            "search_pattern must be one of current, lawnmower, spiral, two_altitude"
        )
    low_search_altitude = None
    obstacles = load_sdf_obstacles(world_path) if world_path else ()
    # Keep the final arbiter on the same typed clearance source as the
    # executor/loop.  Optional depth sources can be composed by callers
    # before runtime construction; an empty map remains byte-compatible.
    self.safety_envelope.obstacle_source = CompositeSource((StaticMapSource(obstacles),))
    # Size-aware search altitude: low enough that an object of the task's typical
    # size spans about min_search_px pixels at a mid-image depression angle.
    if search_pattern == "two_altitude":
        low_search_setting = float(options.get("low_search_altitude_m", 12.0))
        if not math.isfinite(low_search_setting) or low_search_setting <= 0:
            raise ValueError("low_search_altitude_m must be finite and positive")

    def size_aware_altitude(text: str) -> float | None:
        if not options.get("size_aware_search"):
            return None
        size = typical_object_size(text)
        depression = mount_pitch + math.radians(20.0)
        needed = size * float(camera_info["fx"]) / float(options.get("min_search_px", 14.0))
        search_altitude = max(
            float(options.get("min_search_altitude_m", 8.0)),
            min(altitude_m, needed * math.sin(depression)),
        )
        # Never search below the known obstacles: at 7.9 m over a counting
        # site with a 7.2 m house the forward legs were blocked twice, the
        # loop stopped searching and a cone was never seen again.
        if obstacles:
            clearance = float(options.get("search_obstacle_clearance_m", 2.0))
            if not math.isfinite(clearance) or clearance < 0:
                raise ValueError("search_obstacle_clearance_m must be finite and >= 0")
            floor = max(obstacle.height_m for obstacle in obstacles) + clearance
            search_altitude = max(search_altitude, min(float(altitude_m), floor))
        return search_altitude

    def fixed_wing_search(home, radius_m):
        # Aircraft that cannot hover fly lawnmower lanes instead of turning in place.
        profile = self.airframe_profile
        return FixedWingSearch(
            home_ned=home,
            radius_m=radius_m,
            footprint_width_m=camera_footprint_width_m(
                camera_info, self.search_altitude_m or altitude_m, fw_mount_pitch
            ),
            turn_radius_m=profile.min_turn_radius_m or profile.orbit_radius(),
            max_leg_m=profile.limits.max_step_distance_m,
            camera_pitch_rad=fw_mount_pitch,
            max_search_calls=100,
            return_reserve_calls=40,
            coverage_enabled=True,
            camera_intrinsics=camera_info,
            camera_image_size=image_size,
            camera_is_gimballed=(
                profile.camera_mount is not None and profile.camera_mount.type == "gimbal"
            ),
        )

    def build_search(text: str) -> None:
        """Derive the task-size search altitude and the shared search policies."""
        nonlocal low_search_altitude
        self.search_altitude_m = size_aware_altitude(text)
        if search_pattern == "two_altitude":
            low_search_altitude = min(
                low_search_setting, self.search_altitude_m or float(altitude_m)
            )
        # The search pattern is target-blind; a target placed far outside the
        # detection range keeps ExpertSearchPolicy in its search/scan states.
        self._search_mc = ExpertSearchPolicy(
            target_ned=(1.0e7, 1.0e7, 0.0),
            intrinsics=camera_info,
            image_size=image_size,
            altitude_m=self.search_altitude_m or altitude_m,
            obstacles=obstacles,
            home_ned=home_ned or (0.0, 0.0, 0.0),
            camera_pitch_rad=mc_mount_pitch,
            search_pattern=search_pattern,
            **(
                {"low_altitude_m": low_search_altitude}
                if low_search_altitude is not None
                else {}
            ),
        )
        self._search_fw = fixed_wing_search(
            home_ned or (0.0, 0.0, 0.0), self._search_mc.search_radius_m
        )
        if not plans_like_multirotor:
            self._search = self._search_fw
        elif hybrid_vtol:
            self._search = self._search_fw
        else:
            self._search = self._search_mc

    build_search(self.task_text)
    camera = CameraModel(
        fx=float(camera_info["fx"]),
        fy=float(camera_info["fy"]),
        cx=float(camera_info["cx"]),
        cy=float(camera_info["cy"]),
        width=image_size[0],
        height=image_size[1],
        pitch_down_rad=mount_pitch,
    )
    confirm_min_side_px = float(options.get("confirm_min_side_px", 20.0))
    second_look = options.get("second_look_after_steps")
    loop_defaults: dict[str, Any] = {
        "confirm_min_side_px": confirm_min_side_px,
        "contradiction_relook_enabled": True,
        # A VTOL's back-transition climbs (the SITL tailsitter rose from 30 m to
        # 60 m): a hover-mode search first settles back to the mission altitude.
        "search_altitude_m": (
            float(altitude_m)
            if hover_ops and self.search_altitude_m is None
            else self.search_altitude_m
        ),
        "near_depth": near_depth_enabled,
        **position_defaults,
    }
    # The task semantics table is onboard-available and already drives the
    # size-aware search.  Reuse it for the large-object ground-anchor
    # correction; an unknown task leaves the correction disabled.
    typical_size = typical_object_size(self.task_text, default=0.0)
    loop_defaults["ground_extent_size_m"] = typical_size or None
    # Keep the original types: LoopConfig strictly rejects strings, bools
    # masquerading as integers, fractional view counts and non-finite sigma.
    for key in (
        "step_triangulation",
        "step_triangulation_max_views",
        "ground_height_sigma_m",
        "step_triangulation_sigma_scale",
        "step_triangulation_ground_gate_m",
        "step_triangulation_ground_gate_sigma",
        "step_triangulation_parallax_deg_per_m",
        "step_triangulation_min_ray_angle_deg",
        "ground_extent_correction",
        "ground_extent_size_m",
        "ground_extent_min_m",
    ):
        if key in options:
            loop_defaults[key] = options[key]
    if "small_reject_recheck_px" in options:
        value = options["small_reject_recheck_px"]
        loop_defaults["small_reject_recheck_px"] = None if value is None else float(value)
    if "small_reject_recheck_max" in options:
        loop_defaults["small_reject_recheck_max"] = int(options["small_reject_recheck_max"])
    for key, convert in (
        ("contradiction_relook_max", int),
        ("contradiction_relook_views", int),
        ("contradiction_relook_bearing_deg", float),
        ("static_ground_veto_min_m", float),
        ("static_ground_veto_gates", float),
        ("static_verdict_max_shift_m", float),
    ):
        if key in options:
            loop_defaults[key] = convert(options[key])
    if "contradiction_relook_enabled" in options:
        value = options["contradiction_relook_enabled"]
        loop_defaults["contradiction_relook_enabled"] = (
            value.strip().lower() in {"1", "true", "yes", "on"}
            if isinstance(value, str)
            else bool(value)
        )
    if "proposal_min_side_px" in options:
        min_side = float(options["proposal_min_side_px"])
        if not math.isfinite(min_side) or min_side <= 0:
            raise ValueError("proposal_min_side_px must be finite and positive")
        loop_defaults["min_box_side_px"] = min_side
    if second_look is not None:
        loop_defaults["second_look_after_steps"] = int(second_look)
    if options.get("confirm_max_range_m") is not None:
        loop_defaults["confirm_max_range_m"] = float(options["confirm_max_range_m"])
    if "closest_view_identity_matches" in options:
        value = options["closest_view_identity_matches"]
        loop_defaults["closest_view_identity_matches"] = None if value is None else int(value)
    if options.get("second_look_max_confidence") is not None:
        loop_defaults["second_look_max_confidence"] = float(
            options["second_look_max_confidence"]
        )
    if "memory_enabled" in options:
        value = options["memory_enabled"]
        loop_defaults["memory_enabled"] = (
            value.strip().lower() in {"1", "true", "yes", "on"}
            if isinstance(value, str)
            else bool(value)
        )
    if hybrid_vtol:
        loop_defaults["vtol_fw_reentry_min_mc_steps"] = int(
            options.get("vtol_fw_reentry_min_mc_steps", 3)
        )
        loop_defaults["vtol_fw_transition_budget"] = int(
            options.get("vtol_fw_transition_budget", 3)
        )
        loop_defaults["vtol_fw_reentry_candidate_max_steps"] = int(
            options.get("vtol_fw_reentry_candidate_max_steps", 3)
        )
    for key, convert in (
        ("ground_reid_radius_m", float),
        ("appearance_size_ratio", float),
        ("appearance_aspect_ratio", float),
        ("appearance_color_distance", float),
        ("memory_radius_m", float),
        ("memory_footprint_factor", float),
        ("memory_min_verdicts", int),
        ("memory_max_range_ratio", float),
    ):
        if key in options:
            loop_defaults[key] = convert(options[key])
    if "ground_reid_max_steps" in options:
        value = options["ground_reid_max_steps"]
        loop_defaults["ground_reid_max_steps"] = None if value is None else int(value)
    if not self.motion_adapter.is_multirotor:
        # An aircraft that cannot hover never settles on home: the autopilot's
        # RTL flies it home and lands once the loop has nothing left to do.  A
        # VTOL hands it over too: PX4's VTOL RTL transitions, flies home and
        # lands vertically, which the 15 m multirotor home legs would not.
        loop_defaults["autopilot_return"] = True
    if self.airframe_profile.category == "fixed_wing":
        # A plane cannot turn toward a candidate in place: it flies fly-by
        # passes sized from its turn radius and the forward camera's FOV.
        loop_defaults["fixed_wing_pass"] = True
        loop_defaults["fw_min_turn_radius_m"] = self.airframe_profile.loiter_radius_m()
        for key, convert in (
            ("fw_pass_view_range_m", float),
            ("fw_pass_fov_margin_deg", float),
            ("fw_pass_max_laps", float),
            ("fw_pass_max_steps", int),
        ):
            if key in options:
                loop_defaults[key] = convert(options[key])

    def make_loop(task_text: str, region=None, **overrides: Any) -> OnboardLoop:
        restart_route = bool(overrides.pop("restart_route", True))
        search = self._search
        search_mc = self._search_mc
        for policy in (search, search_mc):
            if isinstance(policy, ExpertSearchPolicy):
                # One shared policy serves every mission step: start each
                # step's route afresh (a finished count left its waypoint
                # index and coverage_complete outcome for the next step).
                policy.reset()
                policy.restart_route = restart_route
        obstacle_source = StaticMapSource(obstacles)
        if region is not None and (not plans_like_multirotor or hybrid_vtol):
            search = fixed_wing_search(
                (float(region[0]), float(region[1]), 0.0), float(region[2])
            )
            if hybrid_vtol:
                search_mc = ExpertSearchPolicy(
                    target_ned=(1.0e7, 1.0e7, 0.0),
                    intrinsics=camera_info,
                    image_size=image_size,
                    altitude_m=self.search_altitude_m or altitude_m,
                    obstacles=obstacles,
                    home_ned=(float(region[0]), float(region[1]), 0.0),
                    search_radius_m=float(region[2]),
                    camera_pitch_rad=mc_mount_pitch,
                    search_pattern=search_pattern,
                    **(
                        {"low_altitude_m": low_search_altitude}
                        if low_search_altitude is not None
                        else {}
                    ),
                )
        elif region is not None:
            # A bounded zone: the same target-blind pattern, centred on the zone.
            search = ExpertSearchPolicy(
                target_ned=(1.0e7, 1.0e7, 0.0),
                intrinsics=camera_info,
                image_size=image_size,
                altitude_m=self.search_altitude_m or altitude_m,
                obstacles=obstacles,
                home_ned=(float(region[0]), float(region[1]), 0.0),
                search_radius_m=float(region[2]),
                camera_pitch_rad=mc_mount_pitch,
                search_pattern=search_pattern,
                restart_route=restart_route,
                **(
                    {"low_altitude_m": low_search_altitude}
                    if low_search_altitude is not None
                    else {}
                ),
            )
        def search_action(pose, yaw):
            policy = self._active_search(search, search_mc)
            if isinstance(policy, FixedWingSearch) and policy.camera_is_gimballed:
                return policy.next_action(
                    pose_ned=pose, yaw_deg=yaw, camera_pose=self._search_camera_pose
                )
            # Fixed mounts retain the legacy coverage geometry and actions.
            return policy.next_action(pose_ned=pose, yaw_deg=yaw)

        loop = OnboardLoop(
            camera=camera,
            search_action=search_action,
            search_outcome=lambda: getattr(
                self._active_search(search, search_mc), "search_outcome", None
            ),
            segment_clear=lambda pose, n, e: segment_clear_of_obstacles(
                pose, (n, e, pose[2]), max(0.0, -float(pose[2])), obstacles
            ),
            obstacle_source=obstacle_source,
            config=LoopConfig(**{**loop_defaults, **overrides}),
        )
        if home_ned is not None:
            loop.home = home_ned[:2]
            loop.cruise_altitude_m = altitude_m
        if loop.config.fixed_wing_pass:
            # Plan fly-by orbits inside the same fences the final arbiter
            # enforces, so a pass is shrunk instead of refused into a loiter.
            loop.pass_clear = self.safety_envelope.leg_allowed
        return loop

    def retarget_search(text: str) -> None:
        """Size the search for a task that arrives after start-up.

        An operator-link policy starts on a placeholder sentence ("operator
        link mission"), so the size-aware search fell back to the 2 m
        default object: e2e run10 counted cones from 13.0 m on 24 m rings
        where the suite, sized for a 0.5 m cone, flew 11.3 m and 20 m.
        """
        build_search(text)
        loop_defaults["search_altitude_m"] = (
            float(altitude_m)
            if hover_ops and self.search_altitude_m is None
            else self.search_altitude_m
        )
        if "ground_extent_size_m" not in options:
            loop_defaults["ground_extent_size_m"] = (
                typical_object_size(text, default=0.0) or None
            )

    self._retarget_search = retarget_search
    self.loop = make_loop(task_text)
    return make_loop


def assemble_runtime_runners(
    self: Any,
    *,
    options: dict[str, Any],
    mission: str | None,
    proposer: Callable | None,
    slow_runner: Any,
    proposer_factory: Callable[..., Any],
    health_monitor_factory: Callable[..., Any],
    path_type: Callable[..., Path],
    planning_model_spec: Callable[..., Any],
    model_sha256: Callable[..., str],
    load_runner_type: Callable[[], Any],
) -> RuntimeRunnerState:
    """Assemble proposer, slow runners, restart state, and health monitoring."""
    self.proposer = (
        proposer
        if proposer is not None
        else proposer_factory(
            path_type(options["yoloe_weights"]),
            confidence=float(options.get("confidence", 0.05)),
            imgsz=int(options.get("imgsz", 1280)),
            tiled=options.get("proposer_tiled", False),
            tile_overlap=float(options.get("proposer_tile_overlap", 0.2)),
            merge_iou=float(options.get("proposer_merge_iou", 0.5)),
        )
    )
    LlamaServerRunner = load_runner_type()

    identity_port = int(options.get("port", 9941))
    self._identity_port = identity_port
    planning_spec = planning_model_spec(
        options.get("planning_model"), identity_mmproj=options["mmproj"]
    )
    split_planning = mission in {"from_text", "operator_link"} and planning_spec is not None
    startup_planning = mission == "from_text" and planning_spec is not None
    self._model_split = bool(split_planning)
    self._model_lock = threading.RLock()
    self._model_shutdown = threading.Event()
    self._model_switching = False
    self._planning_model_path = planning_spec[0] if planning_spec else None
    self._planning_mmproj = planning_spec[1] if planning_spec else None
    model_hash_cache = self.work_dir / ".model-hash-cache.json"
    self._model_runtime: dict[str, Any] = {
        "enabled": bool(split_planning),
        "planning_model_path": planning_spec[0] if planning_spec else None,
        "planning_model_sha256": (
            model_sha256(planning_spec[0], cache_path=model_hash_cache)
            if split_planning
            else None
        ),
        "identity_model_path": options["gguf"],
        "identity_model_sha256": (
            model_sha256(options["gguf"], cache_path=model_hash_cache)
            if split_planning
            else None
        ),
        "planning_fallback_to_identity": False,
        "swap_duration_s": None,
        "swap_completed_monotonic_s": None,
    }

    # llama-server keeps evicted prompt states in host RAM (8 GiB by default).
    # Every identity call carries a new crop, so the cache never hits and only
    # grows: it starved a 32 GB SITL host until CUDA launches failed, and an
    # 8 GB Jetson cannot hold it at all.  0 turns it off.
    cache_ram_mib = int(options.get("llama_cache_ram_mib", 0))
    llama_batch_size = options.get("llama_batch_size")
    llama_ubatch_size = options.get("llama_ubatch_size")

    initial_startup_timeout_s = float(
        (options.get("health") or {}).get("slow_initial_startup_timeout_s", 180.0)
        if isinstance(options.get("health"), dict)
        else 180.0
    )
    restart_startup_timeout_s = float(
        (options.get("health") or {}).get("slow_restart_startup_timeout_s", 30.0)
        if isinstance(options.get("health"), dict)
        else 30.0
    )

    def make_identity_runner(startup_timeout_s: float):
        return LlamaServerRunner(
            path_type(options["gguf"]),
            path_type(options["mmproj"]),
            executable=path_type(options["llama_server"]),
            port=identity_port,
            max_new_tokens=128 if split_planning else (768 if mission == "from_text" else 128),
            crop_policy="candidate_zoom_v2",
            identity_only=True,
            logprobs=True,
            fast_identity=str(options.get("fast_identity", False)).lower()
            in {"1", "true", "yes"},
            cache_ram_mib=cache_ram_mib,
            log_dir=path_type(options.get("llama_log_dir", self.work_dir / "mission_log")),
            keep_logs=int(options.get("llama_keep_logs", 3)),
            startup_timeout_s=startup_timeout_s,
            batch_size=llama_batch_size,
            ubatch_size=llama_ubatch_size,
        )

    self._make_identity_runner = lambda: make_identity_runner(restart_startup_timeout_s)

    def start_identity_runner():
        if slow_runner is not None:
            return slow_runner
        deadline = time.monotonic() + 10.0
        while True:
            try:
                return make_identity_runner(initial_startup_timeout_s)
            except Exception as exc:
                address_busy = (
                    "address already in use" in str(exc).lower()
                    or "address in use" in str(exc).lower()
                )
                if not address_busy or time.monotonic() >= deadline:
                    health = getattr(self, "health", None)
                    if health is not None:
                        health.record_planning_failure()
                    raise
                time.sleep(0.1)

    planner_start_error: Exception | None = None
    if startup_planning:
        try:
            self.slow = LlamaServerRunner(
                path_type(planning_spec[0]),
                path_type(planning_spec[1]) if planning_spec[1] is not None else None,
                executable=path_type(options["llama_server"]),
                port=identity_port,
                max_new_tokens=768,
                crop_policy="none",
                identity_only=False,
                logprobs=False,
                wait_port_free=True,
                cache_ram_mib=cache_ram_mib,
                log_dir=path_type(options.get("llama_log_dir", self.work_dir / "mission_log")),
                keep_logs=int(options.get("llama_keep_logs", 3)),
                batch_size=llama_batch_size,
                ubatch_size=llama_ubatch_size,
            )
        except Exception as exc:
            planner_start_error = exc
            self._model_runtime["planning_fallback_to_identity"] = True
            self.slow = start_identity_runner()
    elif slow_runner is None:
        self.slow = make_identity_runner(initial_startup_timeout_s)
    else:
        self.slow = slow_runner
    health_options = options.get("health") if isinstance(options.get("health"), dict) else {}
    self._health_labels_enabled = bool(health_options.get("enabled", False))
    self.slow_call_timeout_s = float(health_options.get("slow_timeout_s", 15.0))
    # Seconds to keep trying to revive the identity runner after a slow
    # call failed (transport fault or crash).  0 keeps the previous
    # behaviour: the failure propagates immediately.
    self.slow_restart_budget_s = float(health_options.get("slow_restart_budget_s", 2.0))
    self.slow_restart_limit = max(0, int(health_options.get("slow_restart_limit", 1)))
    self.slow_restart_interval_s = float(health_options.get("slow_restart_interval_s", 5.0))
    self._slow_restart_attempts = 0
    self._slow_restart_next_retry = 0.0
    self._slow_restart_startup_timeout_s = restart_startup_timeout_s
    self._slow_restart_thread = None
    self._slow_restart_result = None
    self._slow_restart_error = None
    self._slow_restart_generation = 0
    self._slow_restart_shutdown_timeout_s = float(
        ((options.get("health") or {}).get("slow_restart_shutdown_timeout_s", 5.0))
        if isinstance(options.get("health"), dict) else 5.0
    )
    # The policy owns retry decisions so a request timeout cannot trigger
    # LlamaServerRunner's generic three-failure restart behavior.
    self.slow.max_restarts = 0
    self.slow.request_timeout_s = self.slow_call_timeout_s
    health_defaults = {
        "enabled": True,
        "slow_failure_limit": 1,
        "escalate_after_s": 10.0,
    }
    health_defaults.update(health_options)
    self.health = health_monitor_factory(
        config=HealthConfig(
            **{
                k: v
                for k, v in health_defaults.items()
                if k in HealthConfig.__dataclass_fields__
            }
        ),
        runner=self.slow,
        disk_path=self.work_dir,
    )
    self._slow_failed_current = False
    self._slow_skipped_current = False
    self._slow_unavailable_reason = None
    self._health_recovery_executor = None
    return RuntimeRunnerState(split_planning, planner_start_error, start_identity_runner)


def assemble_runtime_executor(
    self: Any,
    *,
    options: dict[str, Any],
    mission: str | None,
    capabilities: frozenset[str],
    make_loop: Callable[..., OnboardLoop],
    altitude_m: float,
    home_ned: tuple[float, float, float] | None,
    runtime_mode: str | None,
    runner_state: RuntimeRunnerState,
    build_mission_from_text: Callable[..., Any],
    perception_task_text: Callable[..., str],
    load_http_json: Callable[[], Callable[..., Any]],
    enabled: Callable[[Any], bool],
) -> None:
    """Compile/admit the startup plan and assemble its executor and supervisor."""
    split_planning = runner_state.split_planning
    planner_start_error = runner_state.planner_start_error
    start_identity_runner = runner_state.start_identity_runner
    self.mission_plan: MissionPlan | None = None
    self.mission_plan_report: dict[str, Any] | None = None
    self._mission_plan_labeled = False
    # Optional multi-skill mode: a MissionPlan compiled from the complete task sentence.
    self.executor: MissionExecutor | MissionSupervisor | None = None
    self.operator_link_waiting = False
    self.supervisor: MissionSupervisor | None = None
    self.downlink: DownlinkQueue | None = None
    executor_config = ExecutorConfig(
        step_budget=int(options.get("step_budget", 60)),
        count_budget_max=int(options.get("count_budget_max", 4096)),
        runtime_step_limit=(
            None
            if options.get("runtime_step_limit") is None
            else int(options["runtime_step_limit"])
        ),
        landing_descent_speed_mps=float(options.get("landing_descent_speed_mps", 1.0)),
        alert_min_identity_matches=int(options.get("alert_min_identity_matches", 1)),
        alert_require_majority=(
            options.get("alert_require_majority", False)
            if not isinstance(options.get("alert_require_majority", False), str)
            else options["alert_require_majority"].strip().lower() in {"1", "true", "yes", "on"}
        ),
        alert_cluster_radius_m=float(options.get("alert_cluster_radius_m", 4.0)),
        # Patrol/monitor closer looks and grown small-rejection re-asks
        # (0 / null turn them off for an A/B run).
        alert_look_attempts=int(options.get("alert_look_attempts", 3)),
        alert_grown_reask_ratio=(
            None
            if options.get("alert_grown_reask_ratio", 1.25) is None
            else float(options.get("alert_grown_reask_ratio", 1.25))
        ),
        deliver_min_altitude_m=float(options.get("deliver_min_altitude_m", 1.0)),
        deliver_max_altitude_m=float(options.get("deliver_max_altitude_m", 5.0)),
        deliver_altitude_m=float(options.get("deliver_altitude_m", 2.0)),
        deliver_position_tolerance_m=float(options.get("deliver_position_tolerance_m", 1.0)),
        deliver_hold_s=float(options.get("deliver_hold_s", 2.0)),
        deliver_ack_timeout_s=float(options.get("deliver_ack_timeout_s", 10.0)),
    )
    if mission:
        if mission == "operator_link":
            # Keep a valid executor shape for the collector while the
            # authenticated operator link waits for its first package.
            self.operator_link_waiting = True
            plan = plan_for_find_task("operator mission")
        elif mission == "package":
            if self.mission_package is None:
                raise ValueError("mission=package requires mission_package")
            plan = self.mission_package.plan
            self.mission_plan = plan
            report = {"error": None, "source": "package", "repairs": []}
        elif mission == "find":
            plan = plan_for_find_task("target")
        elif mission == "count":
            plan = plan_for_count_task("target", region=str(options.get("region", "zone:all")))
        elif mission == "find_and_inspect":
            plan = plan_for_find_and_inspect(
                "target",
                views=int(options.get("inspect_views", 4)),
                standoff_m=float(options.get("inspect_standoff_m", 12.0)),
            )
        elif mission == "from_text":
            _http_json = load_http_json()

            refs = options.get("refs") or {}

            labels = options.get("ref_labels") or {}
            retry_setting = options.get(
                "retry_on_package_violation",
                options.get(
                    "retry_on_package_violations",
                    options.get("mission_package_retry", False),
                ),
            )
            package_retry = (
                retry_setting.strip().lower() in {"1", "true", "yes", "on"}
                if isinstance(retry_setting, str)
                else bool(retry_setting)
            )
            plan_checker = (
                (
                    lambda candidate: check_plan_against_package(
                        candidate,
                        self.mission_package,
                        capabilities=capabilities,
                        executor_config=executor_config,
                    )
                )
                if package_retry and self.mission_package is not None
                else None
            )

            def compile_once(runner):
                post_attempted = False

                def post(prompt: str, schema: dict[str, Any]) -> str:
                    nonlocal post_attempted
                    post_attempted = True
                    body = {
                        "model": "local",
                        "temperature": 0,
                        "max_tokens": 768,
                        "messages": [{"role": "user", "content": prompt}],
                        "response_format": {
                            "type": "json_schema",
                            "json_schema": {
                                "name": "mission_plan",
                                "strict": True,
                                "schema": schema,
                            },
                        },
                    }
                    started = time.perf_counter()
                    try:
                        response = _http_json(
                            f"http://127.0.0.1:{runner.port}/v1/chat/completions",
                            body,
                            timeout=self.slow_call_timeout_s,
                        )
                    except Exception:
                        self.health.record_slow_call(time.perf_counter() - started, ok=False)
                        raise
                    self.health.record_slow_call(time.perf_counter() - started, ok=True)
                    return response["choices"][0]["message"].get("content", "")

                try:
                    candidate, candidate_report = build_mission_from_text(
                        self.task_text,
                        labelled_refs(refs, labels),
                        post,
                        sorted_keys=bool(options.get("plan_sorted_keys", True)),
                        plan_checker=plan_checker,
                        capabilities=capabilities,
                        include_decision_contract=bool(
                            options.get("planning_decision_contract", False)
                        ),
                        enable_plan_pregate=bool(options.get("planning_pregate", False)),
                    )
                except Exception:
                    # ``post`` records HTTP outcomes; do not count an
                    # exception a second time as a slow fault.
                    self.health.record_planning_failure()
                    if not post_attempted:
                        self.health.record_slow_call(0.0, ok=False)
                    raise
                return candidate, candidate_report

            if split_planning:
                planner_error = planner_start_error
                swap_close_started = None
                if planner_error is not None:
                    self._model_runtime["planning_fallback_to_identity"] = True
                    self.health.record_planning_failure()
                    self.health.record_slow_call(0.0, ok=False)
                    self.health.runner = self.slow
                    plan, report = compile_once(self.slow)
                else:
                    try:
                        plan, report = compile_once(self.slow)
                        if report.get("error"):
                            self.health.record_planning_failure()
                            raise ValueError(
                                f"mission plan compilation failed: {report['error']}"
                            )
                    except Exception:
                        self._model_runtime["planning_fallback_to_identity"] = True
                        swap_close_started = time.monotonic()
                        close = getattr(self.slow, "close", None)
                        if close is not None:
                            close()
                        self.slow = start_identity_runner()
                        self.health.runner = self.slow
                        try:
                            plan, report = compile_once(self.slow)
                        except Exception:
                            close = getattr(self.slow, "close", None)
                            if close is not None:
                                close()
                            raise
                    else:
                        swap_close_started = time.monotonic()
                        close = getattr(self.slow, "close", None)
                        if close is not None:
                            close()
                        self.slow = start_identity_runner()
                        self.health.runner = self.slow
                swap_completed = time.monotonic()
                self._model_runtime["swap_duration_s"] = (
                    swap_completed - swap_close_started
                    if swap_close_started is not None
                    else None
                )
                self._model_runtime["swap_completed_monotonic_s"] = swap_completed
            else:
                try:
                    plan, report = compile_once(self.slow)
                except Exception:
                    raise
            self.mission_plan, self.mission_plan_report = plan, report
            if isinstance(plan, PlanDecision):
                self._record_planner_decision(plan)
            if report.get("error"):
                if split_planning:
                    self.health.record_planning_failure()
                if self.mission_package is not None:
                    package_violations = report.get("package_violations", [])
                    if package_violations:
                        self._refuse_package_plan(package_violations)
                    close = getattr(self.slow, "close", None)
                    if callable(close):
                        close()
                    raise ValueError(
                        f"mission package plan compilation failed: {report['error']}"
                    )
                raise ValueError(f"mission plan compilation failed: {report['error']}")
        else:
            raise ValueError(f"unknown mission template {mission!r}")
        if self.mission_package is not None:
            try:
                self.mission_package.validate_plan(plan)
            except ValueError as exc:
                # The admission layer may safely clamp a small altitude
                # overshoot; retain all other package reference failures.
                if "altitude outside operating limits" not in str(exc):
                    self.slow.close()
                    raise
            admission = validate_plan_for_execution(
                plan,
                self.mission_package,
                self.task_text,
                capabilities=capabilities,
            )
            report["validation"] = {
                "decision": admission.decision,
                "reasons_zh": list(admission.reasons_zh),
                "questions_zh": list(admission.questions_zh),
                "repairs": list(admission.repairs),
            }
            if admission.decision in {"accept", "repair"} and admission.plan is not None:
                plan = self.mission_plan = admission.plan
                report["repairs"] = list(report.get("repairs", ())) + list(admission.repairs)
            else:
                refusal = [{
                    "code": f"PLAN_{admission.decision.upper()}",
                    "step_id": None,
                    "message_zh": "；".join(admission.reasons_zh or ("计划需要操作员确认",)),
                    "message_en": "; ".join(admission.questions_zh or admission.reasons_zh),
                    "questions_zh": list(admission.questions_zh),
                }]
                self.mission_plan_report = report
                self._refuse_package_plan(refusal)
            violations = check_plan_against_package(
                plan,
                self.mission_package,
                capabilities=capabilities,
                executor_config=executor_config,
            )
            if violations:
                self._refuse_package_plan(violations)
            try:
                self.mission_package.validate_plan(plan)
            except ValueError:
                close = getattr(self.slow, "close", None)
                if callable(close):
                    close()
                raise
        self.executor = MissionExecutor(
            plan,
            loop_factory=make_loop,
            refs={str(k): tuple(v) for k, v in (options.get("refs") or {}).items()},
            capabilities=capabilities,
            mission_package=self.mission_package,
            no_fly_zones=(
                self.mission_package.no_fly_zones if self.mission_package is not None else ()
            ),
            release_authorized=enabled(options.get("release_authorized")),
            task_text=(
                (lambda step: perception_task_text(step, self.task_text))
                if mission in {"from_text", "package"}
                else (lambda step: self.task_text)
            ),
            config=executor_config,
            airframe_adapter=self.motion_adapter,
            camera_mount=(
                CameraMountProfile.from_mapping(options["camera_mount"]).to_camera_mount()
                if options.get("camera_mount") is not None
                else None
            ),
            gimbal_controller=options.get("gimbal_controller"),
        )
        # When no package is loaded, use the executor's configured home if
        # it provides one; never infer home from the first observed pose.
        if self.safety_envelope.home_ned is None and self.executor.home is not None:
            self.safety_envelope.set_home((*self.executor.home, 0.0))
        # Set home on the executor itself, before any supervisor wraps it.
        if home_ned is not None:
            self.executor.home = home_ned[:2]
            self.executor.cruise_altitude_m = altitude_m
        if self.mission_package is not None:
            self.executor.cruise_altitude_m = altitude_m
            log = OnboardMissionLog(
                self.work_dir / "mission_log", self.mission_package.mission_id
            )
            self.downlink = DownlinkQueue(log, sender=None)
            self.supervisor = MissionSupervisor(
                self.executor,
                self.mission_package,
                log=log,
                downlink=self.downlink,
                **self._supervisor_link_options(),
                energy_config=EnergyConfig.from_budget(
                    self.mission_package.document["budget"], options.get("energy_model")
                ),
                runtime_mode=runtime_mode,
                checkpoint_path=self.work_dir / "crash_resume" / "checkpoint.json",
            )
            if self._model_split:
                self.downlink.log.append(
                    "model_swap",
                    self.sim_timeline.now
                    if self.sim_timeline is not None
                    else time.monotonic(),
                    model_runtime=dict(self._model_runtime),
                )
            # Fallback for telemetry without a vehicle-clock stamp: readers
            # stamp receipts with host monotonic time, while SIM timelines run
            # on the simulation clock. Stamped SIM samples bypass this offset
            # (see _energy_vehicle_estimator); it drifts when the sim is slow.
            self._energy_receipt_clock_offset = self.supervisor._now - time.monotonic()
            self.executor = self.supervisor
            for event in options.get("ground_events", []):
                self.supervisor.ground_event(event)
    self.slow_calls = 0
