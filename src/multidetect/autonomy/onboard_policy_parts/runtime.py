"""Runtime assembly helpers for the onboard policy."""

from __future__ import annotations

import time
import warnings
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from multidetect.autonomy.capability_contract import (
    capabilities_for_airframe,
    effective_capabilities,
    normalize_capabilities,
)
from multidetect.autonomy.airframe_profile import CameraMountProfile
from multidetect.autonomy.energy_estimator import EnergyConfig
from multidetect.autonomy.mission_executor import MissionExecutor
from multidetect.autonomy.mission_package import (
    MissionPackage,
    check_plan_against_package,
    load_mission_package,
)
from multidetect.autonomy.mission_plan import PlanDecision
from multidetect.autonomy.mission_supervisor import MissionSupervisor
from multidetect.autonomy.onboard_mission_log import DownlinkQueue, OnboardMissionLog
from multidetect.autonomy.onboard_policy_parts.helpers import labelled_refs
from multidetect.autonomy.onboard_policy_parts.planning_http import planning_http
from multidetect.autonomy.safety_envelope import SafetyEnvelope, SafetyEnvelopeConfig


def build_safety_envelope(
    options: Mapping[str, Any],
    airframe_profile: Any,
    motion_adapter: Any,
    mission_package: Any,
    home_ned: tuple[float, float, float] | None,
    ground_projection: Any,
) -> SafetyEnvelope:
    """Build the final policy envelope from airframe and package constraints."""
    # Package geometry is authoritative for home and operating area; explicit
    # safety settings may tighten those limits further.
    envelope_source = dict(options)
    if airframe_profile.category != "multirotor":
        dynamics = motion_adapter.safety.vehicle
        envelope_source["vehicle"] = {
            "category": dynamics.category,
            "can_hover": dynamics.can_hover,
            "min_airspeed_mps": dynamics.min_airspeed_mps,
            "cruise_airspeed_mps": dynamics.cruise_airspeed_mps,
            "max_airspeed_mps": dynamics.max_airspeed_mps,
            "min_turn_radius_m": dynamics.min_turn_radius_m,
            "max_bank_deg": dynamics.max_bank_deg,
            "climb_rate_mps": dynamics.climb_rate_mps,
            "descent_rate_mps": dynamics.descent_rate_mps,
        }
        # The multirotor-sized default clips fixed-wing search and turn-off legs.
        envelope_source.setdefault(
            "max_translation_m", airframe_profile.limits.max_step_distance_m
        )
    if mission_package is not None:
        area = mission_package.document["operating_area"]
        package_polygon = (
            (float(area["north_min_m"]), float(area["east_min_m"])),
            (float(area["north_max_m"]), float(area["east_min_m"])),
            (float(area["north_max_m"]), float(area["east_max_m"])),
            (float(area["north_min_m"]), float(area["east_max_m"])),
        )
        envelope_source.setdefault("operating_polygon", package_polygon)
        envelope_source.setdefault("min_altitude_m", float(area["min_altitude_m"]))
        envelope_source.setdefault("max_altitude_m", float(area["max_altitude_m"]))
        raw_home = tuple(float(value) for value in mission_package.document["home"])
        package_home = raw_home if len(raw_home) == 3 else (*raw_home, 0.0)
    else:
        package_home = None
    return SafetyEnvelope(
        SafetyEnvelopeConfig.from_mapping(envelope_source),
        home_ned=(tuple(float(value) for value in home_ned) if home_ned is not None else package_home),
        ground_model=getattr(ground_projection, "model", None),
    )


def load_runtime_mission_package(
    options: dict[str, Any],
    *,
    runtime_mode: str | None,
    mission_package: MissionPackage | None,
    altitude_m: float,
    airframe_profile: Any,
    enabled: Any,
    root: Path,
) -> tuple[MissionPackage | None, Any, frozenset[str], frozenset[str]]:
    """Resolve package security, package metadata, and effective capabilities."""
    package_raw = options.get("mission_package")
    if runtime_mode and str(runtime_mode).lower() in {"real", "flight", "hil", "live"} and package_raw is None:
        raise ValueError("real/flight runtime requires a signed mission_package")
    security = options.get("security") if isinstance(options.get("security"), dict) else {}
    mission_key_setting = options.get("mission_package_verify_key") or security.get(
        "mission_package_verify_key"
    )
    legacy_key_setting = (
        options.get("operator_key_path")
        or options.get("mission_key_path")
        or options.get("mission_signing_key_path")
        or security.get("key_path")
    )
    key_setting = mission_key_setting or legacy_key_setting
    if legacy_key_setting is not None and mission_key_setting is None:
        warnings.warn(
            "operator_key_path is deprecated; use mission_package_verify_key",
            DeprecationWarning,
            stacklevel=3,
        )
    ground_key_setting = (
        options.get("ground_command_verify_key")
        or security.get("ground_command_verify_key")
        or legacy_key_setting
    )
    if isinstance(key_setting, str):
        key_path = Path(key_setting)
        key_setting = key_path if key_path.is_absolute() else root / key_path
    if isinstance(package_raw, str):
        package_path = Path(package_raw)
        package_raw = package_path if package_path.is_absolute() else root / package_path
    package = mission_package or (
        load_mission_package(
            package_raw,
            key_path=key_setting,
            require_signature=key_setting is not None,
            mode=runtime_mode,
        )
        if package_raw is not None
        else None
    )
    if package is not None:
        options["refs"] = package.refs
        options["ref_labels"] = package.document.get("ref_labels", {})
        options["task_text"] = package.document.get("task_text") or package.plan.goal
        options["mission"] = "package" if package.plan is not None else "from_text"
        area = package.document["operating_area"]
        if not area["min_altitude_m"] <= altitude_m <= area["max_altitude_m"]:
            raise ValueError("launch altitude outside mission package limits")
    configured_capabilities = set(options.get("capabilities", ()))
    if enabled(options.get("payload_capability")):
        configured_capabilities.add("payload_drop")
    airframe_capabilities = capabilities_for_airframe(airframe_profile, configured_capabilities)
    package_capabilities = (
        normalize_capabilities(package.document.get("capabilities", ()))
        if package is not None
        else None
    )
    return (
        package,
        ground_key_setting,
        airframe_capabilities,
        effective_capabilities(airframe_capabilities, package_capabilities),
    )


class RuntimeAdmissionMixin:
    """Package admission support that keeps all dependencies explicit here."""

    def _supervisor_link_options(self) -> dict[str, Any]:
        options = self.config
        security = options.get("security") if isinstance(options.get("security"), dict) else {}
        key = self._ground_key_setting
        return {
            **({"clock": lambda: self.sim_timeline.now} if self.sim_timeline else {}),
            "security_key_path": str(key) if key is not None else None,
            "timestamp_window_s": float(
                options.get("command_timestamp_window_s", security.get("timestamp_window_s", 30.0))
            ),
            "clock_skew_s": float(options.get("command_clock_skew_s", security.get("clock_skew_s", 5.0))),
            "rate_limit": int(options.get("command_rate_limit", security.get("rate_limit", 20))),
            "rate_window_s": float(options.get("command_rate_window_s", security.get("rate_window_s", 10.0))),
        }

    def _refuse_package_plan(
        self,
        violations: Sequence[Mapping[str, Any]],
        package: MissionPackage | None = None,
        *,
        close_runner: bool = True,
    ) -> None:
        package = package if package is not None else self.mission_package
        if package is None:
            raise ValueError("mission plan violates package policy")
        log = None
        try:
            log = OnboardMissionLog(self.work_dir / "mission_log", package.mission_id)
            now = self.sim_timeline.now if self.sim_timeline is not None else time.monotonic()
            log.append(
                "mission_refused", now, reason="package_policy_violation", violations=list(violations)
            )
            log.finish(
                now,
                {"status": "refused", "reason": "package_policy_violation", "violations": list(violations)},
            )
        finally:
            close = getattr(self.slow, "close", None) if close_runner else None
            if close is not None:
                close()
        details = "; ".join(
            f"{item.get('code')}: {item.get('message_zh')} / {item.get('message_en')}"
            for item in violations
        )
        raise ValueError(f"mission plan refused by package policy: {details}")

    def _record_planner_decision(
        self, decision: PlanDecision, package: MissionPackage | None = None
    ) -> None:
        payload = asdict(decision)
        self.mission_plan = None
        self.mission_plan_report = {"error": None, "decision": payload, "raw": payload}
        package = package or self.mission_package
        if package is not None:
            log = OnboardMissionLog(self.work_dir / "mission_log", package.mission_id)
            now = self.sim_timeline.now if self.sim_timeline is not None else time.monotonic()
            log.append("mission_refused", now, reason="planner_decision", decision=payload)
            log.finish(now, {"status": "refused", "reason": "planner_decision", "decision": payload})
            active = getattr(self, "supervisor", None)
            active_log = getattr(active, "log", None)
            downlink = getattr(active, "downlink", None)
            if active_log is not None and active_log.mission_id != package.mission_id:
                active_log.append(
                    "mission_refused", now, requested_mission_id=package.mission_id,
                    reason="planner_decision", decision=payload,
                )
                if downlink is not None:
                    downlink.notify()
        raise ValueError(f"mission planner {decision.decision}: {decision.reason}")

    def install_operator_package(self, supervisor: MissionSupervisor) -> None:
        if not isinstance(supervisor, MissionSupervisor):
            raise TypeError("operator package candidate must be a MissionSupervisor")
        self.mission_package, self.mission_plan = supervisor.package, supervisor.executor.plan
        self.mission_plan_report = getattr(supervisor, "plan_report", None)
        task_text = str(supervisor.package.document.get("task_text") or self.mission_plan.goal)
        self.task_text = task_text
        retarget = getattr(self, "_retarget_search", None)
        if retarget is not None:
            queries = [
                str(step.params["query"])
                for step in self.mission_plan.steps
                if isinstance(step.params.get("query"), str)
            ]
            retarget(" ".join([task_text, *queries]))
        self.supervisor, self.executor, self.downlink = supervisor, supervisor, supervisor.downlink
        self.operator_link_waiting = False

    def acknowledge_payload_release(self, request_id: str, *, released: bool = True) -> bool:
        return self.executor is not None and self.executor.acknowledge_payload_release(
            request_id, released=released
        )


def prepare_runtime_operator_package(
    self: Any,
    package: MissionPackage,
    *,
    build_mission_from_text: Callable[..., Any],
    perception_task_text: Callable[..., str],
    path_type: Callable[..., Path],
    load_runner_type: Callable[[], Any],
    load_http_json: Callable[[], Callable[..., Any]],
) -> MissionSupervisor:
    """Plan with serialized model switching, then build an uninstalled candidate."""
    if self.executor is None:
        raise RuntimeError("operator package arrived before policy initialization")
    capabilities = effective_capabilities(
        getattr(self, "airframe_capabilities", frozenset()),
        normalize_capabilities(package.document.get("capabilities", ())),
    )
    if package.plan is not None:
        plan = package.plan
        report: dict[str, Any] = {"error": None, "source": "package"}
    else:
        _http_json = load_http_json()
        LlamaServerRunner = load_runner_type()

        planning_runner = None

        def post(prompt: str, schema: dict[str, Any]) -> str:
            runner = planning_runner or self.slow
            if runner is None:
                raise ConnectionRefusedError("慢模型不可用: planner runner unavailable")
            response = planning_http(runner, _http_json,
                f"http://127.0.0.1:{runner.port}/v1/chat/completions",
                {
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
                },
                timeout=self.slow_call_timeout_s,
                runner_type=LlamaServerRunner,
            )
            return response["choices"][0]["message"].get("content", "")

        if self._model_split:
            active_supervisor = getattr(self, "supervisor", None)
            if active_supervisor is not None:
                active_supervisor.planned_model_switch = True
            with self._model_lock:
                self._model_switching = True
            self._cancel_slow_restart()
            restart_thread = self._slow_restart_thread
            if restart_thread is not None and restart_thread.is_alive():
                with self._model_lock:
                    self._model_switching = False
                raise RuntimeError("慢模型重建仍在运行，暂不切换规划模型")
            with self._model_lock:
                started = time.monotonic()
                identity = self.slow
                planner = None
                try:
                    identity_close = getattr(identity, "close", None)
                    if callable(identity_close):
                        identity_close()
                    planner = LlamaServerRunner(
                        path_type(self._planning_model_path),
                        path_type(self._planning_mmproj) if self._planning_mmproj else None,
                        executable=path_type(self.config["llama_server"]), port=self._identity_port,
                        max_new_tokens=768, crop_policy="none", identity_only=False,
                        logprobs=False, wait_port_free=True,
                        cache_ram_mib=int(self.config.get("llama_cache_ram_mib", 0)),
                    )
                    self.slow = planner
                    planning_runner = planner
                    self.health.runner = planner
                    if self._model_shutdown.is_set():
                        raise RuntimeError("operator package planning cancelled")
                    plan, report = build_mission_from_text(
                        str(package.document["task_text"]),
                        labelled_refs(package.refs, package.document.get("ref_labels") or {}),
                        post, capabilities=capabilities,
                        plan_checker=lambda candidate: check_plan_against_package(
                            candidate, package, capabilities=capabilities,
                            executor_config=self.executor.config,
                        ),
                        include_decision_contract=bool(
                            self.config.get("planning_decision_contract", False)
                        ),
                        enable_plan_pregate=bool(self.config.get("planning_pregate", False)),
                    )
                    if report.get("error"):
                        raise ValueError(f"operator task planning failed: {report['error']}")
                    if self._model_shutdown.is_set():
                        raise RuntimeError("operator package planning cancelled")
                except Exception:
                    if self._model_shutdown.is_set():
                        if planner is not None:
                            planner.close()
                            self.slow = None
                        raise RuntimeError("operator package planning cancelled")
                    self._model_runtime["planning_fallback_to_identity"] = True
                    self.health.record_planning_failure()
                    if planner is not None:
                        planner_close = getattr(planner, "close", None)
                        if callable(planner_close):
                            planner_close()
                    self.slow = self._make_identity_runner()
                    planning_runner = self.slow
                    self.health.runner = self.slow
                    plan, report = build_mission_from_text(
                        str(package.document["task_text"]),
                        labelled_refs(package.refs, package.document.get("ref_labels") or {}),
                        post, capabilities=capabilities,
                        plan_checker=lambda candidate: check_plan_against_package(
                            candidate, package, capabilities=capabilities,
                            executor_config=self.executor.config,
                        ),
                        include_decision_contract=bool(
                            self.config.get("planning_decision_contract", False)
                        ),
                        enable_plan_pregate=bool(self.config.get("planning_pregate", False)),
                    )
                else:
                    planner_close = getattr(planner, "close", None)
                    if callable(planner_close):
                        planner_close()
                    self.slow = self._make_identity_runner()
                    planning_runner = self.slow
                    self.health.runner = self.slow
                finally:
                    completed = time.monotonic()
                    self._model_runtime["swap_duration_s"] = completed - started
                    self._model_runtime["swap_completed_monotonic_s"] = completed
                    self._model_switching = False
                    if active_supervisor is not None:
                        active_supervisor.planned_model_switch = False
        else:
            plan, report = build_mission_from_text(
            str(package.document["task_text"]),
            # The same labelled references as a start-up from_text mission.
            # Bare names lost the link from the operator's words to the zone:
            # "数一数场地里…" with "zone:site" alone planned zone:all (e2e
            # run10: a 150 m search, budget exhausted); with "zone:site
            # (场地 / the site)" both planner models choose zone:site.
            labelled_refs(package.refs, package.document.get("ref_labels") or {}),
            post,
            capabilities=capabilities,
            plan_checker=lambda candidate: check_plan_against_package(
                candidate,
                package,
                capabilities=capabilities,
                executor_config=self.executor.config,
            ),
            include_decision_contract=bool(
                self.config.get("planning_decision_contract", False)
            ),
            enable_plan_pregate=bool(self.config.get("planning_pregate", False)),
            )
        if report.get("error"):
            raise ValueError(f"operator task planning failed: {report['error']}")
        if isinstance(plan, PlanDecision):
            self._record_planner_decision(plan, package)
    # The same package policy as a package loaded at start-up, whether the
    # plan came with the package or was compiled here.
    violations = check_plan_against_package(
        plan, package, capabilities=capabilities, executor_config=self.executor.config
    )
    if violations:
        self._refuse_package_plan(violations, package, close_runner=False)
    package.validate_plan(plan)
    executor = MissionExecutor(
        plan,
        loop_factory=self.executor.loop_factory,
        refs=package.refs,
        capabilities=capabilities,
        mission_package=package,
        config=self.executor.config,
        task_text=lambda step: perception_task_text(
            step, package.document.get("task_text", plan.goal)
        ),
        airframe_adapter=self.motion_adapter,
        camera_mount=(
            CameraMountProfile.from_mapping(self.config["camera_mount"]).to_camera_mount()
            if self.config.get("camera_mount") is not None
            else None
        ),
        gimbal_controller=self.config.get("gimbal_controller"),
    )
    executor.home = self.executor.home
    executor.cruise_altitude_m = self.executor.cruise_altitude_m
    log = OnboardMissionLog(self.work_dir / "mission_log", package.mission_id)
    downlink = DownlinkQueue(log, sender=None)
    supervisor = MissionSupervisor(
        executor,
        package,
        log=log,
        downlink=downlink,
        requires_approval=bool(self.config.get("operator_requires_approval", False)),
        runtime_mode=self.runtime_mode,
        **self._supervisor_link_options(),
        # The same onboard energy model as a package loaded at start-up.
        energy_config=EnergyConfig.from_budget(
            package.document["budget"], self.config.get("energy_model")
        ),
    )
    supervisor.plan_report = report
    return supervisor
