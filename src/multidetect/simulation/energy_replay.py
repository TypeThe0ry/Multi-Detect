"""Causal, partial-coverage energy replay of one owned PX4 capture session.

This uses PX4 boot time, not host elapsed time or Gazebo sensor time. The
caller supplies explicit power assumptions; outputs never authorize flight.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime

from ..autonomy.contracts import ContextSnapshot
from ..autonomy.serialization import require_real
from ..clock import Clock, validate_host_identity
from ..px4_mission_feedback import PX4_MISSION_FEEDBACK_CANONICAL_MAX_AGE_S
from ..runtime import RuntimeMode
from ..simulator.flight_model import SimulationEnergyProfile


class CumulativeSimulationEnergy:
    """Persistent energy for one append-only external simulation capture.

    Initial energy is defined at the first complete replay sample, not at boot.
    Replays use the existing flight model's power assumptions. This adapter
    neither mutates vehicle telemetry nor authorizes a subsequent mission.
    """

    def __init__(
        self, profile: SimulationEnergyProfile, *, session_id: str, initial_remaining_wh: float
    ) -> None:
        if not isinstance(profile, SimulationEnergyProfile):
            raise ValueError("explicit SimulationEnergyProfile required")
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("capture session identity required")
        initial = require_real(initial_remaining_wh, "initial_remaining_wh", minimum=0)
        if initial > profile.capacity_wh:
            raise ValueError("initial energy exceeds capacity")
        self._profile = profile
        self._session_id = session_id
        self._initial = initial
        self._history: dict | None = None
        self._replay: dict | None = None
        self._context_identity = None

    @property
    def profile(self) -> SimulationEnergyProfile:
        """Return the immutable model profile used by this capture session."""
        return self._profile

    def project_context(
        self,
        context: ContextSnapshot,
        history: Mapping,
        *,
        session_id: str,
        clock: Clock,
        runtime_mode: RuntimeMode,
    ) -> tuple[ContextSnapshot, dict]:
        """Join simulated energy to an existing fresh shared observation.

        The backend must bind context/session identity and use collector-relative
        time for both Clock and ContextSnapshot. No timestamps or sensor-quality
        fields are fabricated/refreshed here. This is a SIM sensor adapter, not
        a separate planner or a substitute for the mission completion monitor.
        """
        if runtime_mode is not RuntimeMode.SIM:
            raise ValueError("simulated energy projection requires SIM mode")
        if not isinstance(context, ContextSnapshot):
            raise ValueError("existing ContextSnapshot required")
        now = require_real(clock.monotonic(), "clock", minimum=0)
        age = now - context.captured_at_s
        utc_age = (
            clock.utc_now() - datetime.fromisoformat(context.captured_at_utc.replace("Z", "+00:00"))
        ).total_seconds()
        limit = PX4_MISSION_FEEDBACK_CANONICAL_MAX_AGE_S
        if not (0 <= age <= limit and 0 <= utc_age <= limit):
            raise ValueError("fresh same-clock context required")
        identity = (context.airframe_profile_id, context.home_position)
        if self._context_identity is not None and identity != self._context_identity:
            raise ValueError("simulation context identity changed")
        # Validate/join on a candidate so no rejected projection consumes energy.
        candidate = deepcopy(self)
        energy = candidate.observe(history, session_id=session_id)
        positions = [s for s in candidate._history["samples"] if s["type"] == "GLOBAL_POSITION_INT"]
        observed_at = positions[-1]["at_s"]
        if not (0 <= context.captured_at_s - observed_at <= limit and now - observed_at <= limit):
            raise ValueError("energy sample is stale or newer than context")
        projected = replace(
            context,
            battery_remaining_pct=energy["battery_remaining_pct"],
            estimated_flight_time_s=energy["estimated_flight_time_s"],
        )
        energy["energy_observed_at_receive_s"] = observed_at
        energy["input_context_hash"] = context.canonical_hash
        energy["projected_context_hash"] = projected.canonical_hash
        self._history, self._replay = candidate._history, candidate._replay
        self._context_identity = identity
        return projected, energy

    def observe(self, history: Mapping, *, session_id: str) -> dict:
        if session_id != self._session_id:
            raise ValueError("capture session mismatch")
        candidate = deepcopy(dict(history))
        origin = require_real(
            candidate.get("receive_origin_host_monotonic_s"), "receive origin", minimum=0
        )
        label = candidate.get("receive_clock")
        if label == "host_perf_counter_since_collector_start":
            identity = validate_host_identity(candidate.get("host_clock_identity"))
        elif label == "host_monotonic_since_collector_start":
            identity = {"source": "python.monotonic", "session_id": None}
            if candidate.get("host_clock_identity", identity) != identity:
                raise ValueError("legacy collector clock identity mismatch")
        else:
            raise ValueError("collector receive clock required")
        candidate["host_clock_identity"] = deepcopy(identity)
        if self._history is not None:
            previous = self._history["samples"]
            if (
                label != self._history["receive_clock"]
                or identity != self._history["host_clock_identity"]
            ):
                raise ValueError("collector clock changed inside energy session")
            if origin != self._history["receive_origin_host_monotonic_s"]:
                raise ValueError("collector restart inside energy session")
            if candidate.get("samples", [])[: len(previous)] != previous:
                raise ValueError("energy history must be append-only")
        replay = replay_energy_inputs(candidate, self._profile)
        if replay["reason"] != "covered_prefix_complete" or replay["segments"] <= 0:
            gap_details = replay.get("gap_details")
            detail = f"; gap_details={gap_details!r}" if gap_details is not None else ""
            raise ValueError(
                f"continuous energy coverage required: {replay['reason']}{detail}"
            )
        if self._replay is not None and (
            replay["first_covered_boot_ms"] != self._replay["first_covered_boot_ms"]
            or replay["consumed_wh"] < self._replay["consumed_wh"]
        ):
            raise ValueError("energy coverage regressed")
        previous_wh = self._replay["consumed_wh"] if self._replay is not None else 0.0
        remaining = max(0.0, self._initial - replay["consumed_wh"])
        result = {
            "source": "cumulative_simulation_energy_assumption_v1",
            "session_id": self._session_id,
            "receive_clock": label,
            "host_clock_identity": deepcopy(identity),
            "profile": asdict(self._profile),
            "initial_remaining_wh": self._initial,
            "initial_energy_at_boot_ms": replay["first_covered_boot_ms"],
            "consumed_wh": replay["consumed_wh"],
            "newly_consumed_wh": replay["consumed_wh"] - previous_wh,
            "remaining_wh": remaining,
            "battery_remaining_pct": 100 * remaining / self._profile.capacity_wh,
            "estimated_flight_time_s": remaining
            * 3600
            / (self._profile.avionics_power_w + self._profile.propulsion_max_power_w),
            "unserved_energy_wh": max(0.0, replay["consumed_wh"] - self._initial),
            "observed_prefix_boot_s": replay["covered_boot_time_s"],
            "reserve_deducted": False,
            "physical_calibration_claimed": False,
            "authorizes_next_task": False,
        }
        # Commit only after all validation; failed updates preserve prior state.
        self._history, self._replay = candidate, replay
        return result


def replay_energy_inputs(history: Mapping, profile: SimulationEnergyProfile) -> dict:
    """Zero-order hold at the left boot sample; no retrospective state fill.

    The first complete causal sample starts coverage. Missing freshness after
    that point stops it permanently. Leading/trailing unobserved time is never
    estimated, so even successful coverage is not full-mission energy.
    """
    if not isinstance(profile, SimulationEnergyProfile):
        raise ValueError("explicit SimulationEnergyProfile required")
    if history.get("samples_dropped") != 0 or type(history.get("samples_dropped")) is not int:
        raise ValueError("untruncated input history required")
    samples = history.get("samples")
    if not isinstance(samples, list):
        raise ValueError("samples must be a list")
    if type(history.get("samples_seen")) is not int or history["samples_seen"] != len(samples):
        raise ValueError("history count mismatch")
    held: dict[str, tuple[float, int]] = {}
    previous = None
    last_receive = -1.0
    last_boot = None
    segments = 0
    energy_wh = 0.0
    covered_s = 0.0
    first_boot = None
    reason = "no_complete_interval"
    gap_details = None
    for sample in samples:
        at = require_real(sample["at_s"], "at_s", minimum=0)
        if at < last_receive:
            reason = "receive_time_regression"
            break
        last_receive = at
        if any(
            type(sample.get(k)) is not int or sample[k] != 1
            for k in ("source_system", "source_component")
        ):
            reason = "unexpected_source"
            break
        kind = sample.get("type")
        specs = {
            "HEARTBEAT": ("base_mode", 255),
            "VFR_HUD": ("throttle", 100),
            "GLOBAL_POSITION_INT": ("time_boot_ms", 2**32 - 1),
            "SYSTEM_TIME": ("time_boot_ms", 2**32 - 1),
        }
        if kind not in specs:
            reason = "unexpected_message"
            break
        field, maximum = specs[kind]
        value = sample.get("value")
        if sample.get("field") != field or type(value) is not int or not 0 <= value <= maximum:
            reason = "invalid_field"
            break
        if kind in ("HEARTBEAT", "VFR_HUD"):
            held[kind] = (at, value)
            continue
        if kind == "SYSTEM_TIME":
            continue  # Never interleave two independently delivered clock streams.
        if last_boot is not None and value < last_boot:
            reason = "boot_time_not_strictly_increasing"
            break
        if last_boot is not None and value == last_boot:
            # PX4 can deliver the same GLOBAL_POSITION_INT timestamp more than
            # once when the MAVLink stream and the evaluator pump run at
            # different rates.  It carries no elapsed boot interval, so it
            # must be ignored rather than turning an otherwise continuous
            # capture into a false energy-audit failure.  Regressions remain
            # fail-closed above.
            continue
        last_boot = value
        ready = all(k in held and 0 <= at - held[k][0] <= 1.5 for k in ("HEARTBEAT", "VFR_HUD"))
        if previous is not None:
            boot, received, heartbeat, throttle = previous
            dt = (value - boot) / 1000.0
            if (
                not ready
                or at - received > 1.5
                or any(at - s[0] > 1.5 for s in (heartbeat, throttle))
            ):
                reason = "load_or_receive_gap"
                gap_details = {
                    "receive_gap_s": at - received,
                    "left_heartbeat_age_s": at - heartbeat[0],
                    "left_throttle_age_s": at - throttle[0],
                    "current_load_ready": ready,
                }
                break
            if not 0 < dt <= 1.5:
                reason = "boot_time_gap"
                break
            energy_wh += profile.consumed_wh(
                dt, armed=bool(heartbeat[1] & 128), throttle=throttle[1] / 100
            )
            covered_s += dt
            segments += 1
            reason = "covered_prefix_complete"
        if ready:
            if first_boot is None:
                first_boot = value
            previous = (value, at, held["HEARTBEAT"], held["VFR_HUD"])
    return {
        "schema_version": 1,
        "reason": reason,
        "segments": segments,
        "covered_boot_time_s": covered_s,
        "first_covered_boot_ms": first_boot,
        "consumed_wh": energy_wh,
        "profile": asdict(profile),
        "source_clock": "GLOBAL_POSITION_INT.time_boot_ms",
        "stopped_at_receive_s": last_receive,
        "gap_details": gap_details,
        "load_policy": "left_sample_zero_order_hold_max_age_1.5s",
        "full_mission_coverage_claimed": False,
        "physical_calibration_claimed": False,
        "authorizes_next_task": False,
    }
