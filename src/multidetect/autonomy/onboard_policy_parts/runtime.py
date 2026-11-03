"""Runtime assembly helpers for the onboard policy."""

from __future__ import annotations

import time
import warnings
from dataclasses import asdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from multidetect.autonomy.capability_contract import (
    capabilities_for_airframe,
    effective_capabilities,
    normalize_capabilities,
)
from multidetect.autonomy.mission_package import (
    MissionPackage,
    check_plan_against_package,
    load_mission_package,
)
from multidetect.autonomy.mission_plan import PlanDecision
from multidetect.autonomy.mission_supervisor import MissionSupervisor
from multidetect.autonomy.onboard_mission_log import DownlinkQueue, OnboardMissionLog
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
