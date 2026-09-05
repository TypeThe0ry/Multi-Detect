"""Fail-closed actuator mapping and software bench acceptance.

The field gate still requires an operator to check the real servo direction,
travel limits and loss-of-lock return with propulsion disconnected.  This
module closes the deterministic part of that contract first: one canonical
mapping from bounded semantic commands to PWM values, one neutral path for
cancel/loss/watchdog, and one latched failsafe state that cannot accept a new
command until a fresh session is created.

No serial, CAN, GPIO, PWM, MAVLink or Pixhawk transport is present here.  The
bench is deliberately a pure in-memory model so its evidence can be replayed
on every workstation without accidentally driving hardware.  The same mapping
and state transitions can be used by a future hardware adapter after the
propulsion-disconnected bench has been witnessed and hash-bound.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .product_version import PRODUCT_VERSION

SCHEMA_VERSION = 1
EVENT = "actuator_safety_software_acceptance_finished"
VERIFIED_EVENT = "actuator_safety_software_evidence_verified"


class ActuatorSafetyError(ValueError):
    """Raised when a command or software-bench transition is unsafe."""


class ActuatorSafetyState(str, Enum):
    READY = "ready"
    ACTIVE = "active"
    CANCELLED = "cancelled"
    LOST_LOCK_RETURNED = "lost_lock_returned"
    FAILSAFE = "failsafe"


@dataclass(frozen=True, slots=True)
class ServoChannelSpec:
    """One bounded servo channel in the platform-neutral actuator contract."""

    name: str
    channel: int
    minimum_us: int
    neutral_us: int
    maximum_us: int
    direction: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ActuatorSafetyError("servo channel name must be non-empty")
        if (
            isinstance(self.channel, bool)
            or not isinstance(self.channel, int)
            or not 1 <= self.channel <= 32
        ):
            raise ActuatorSafetyError("servo channel must be an integer in [1, 32]")
        values = (self.minimum_us, self.neutral_us, self.maximum_us)
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
            raise ActuatorSafetyError("servo PWM limits must be integers")
        if not self.minimum_us < self.neutral_us < self.maximum_us:
            raise ActuatorSafetyError("servo PWM limits must satisfy minimum < neutral < maximum")
        if isinstance(self.direction, bool) or self.direction not in (-1, 1):
            raise ActuatorSafetyError("servo direction must be +1 or -1")

    def pwm_for(self, command: float) -> int:
        """Map a semantic ``[-1, 1]`` command to this channel's PWM range."""

        if isinstance(command, bool) or not isinstance(command, (int, float)):
            raise ActuatorSafetyError("servo command must be numeric")
        value = float(command)
        if not math.isfinite(value) or not -1.0 <= value <= 1.0:
            raise ActuatorSafetyError("servo command must be finite and within [-1, 1]")
        signed = value * self.direction
        if signed >= 0.0:
            span = self.maximum_us - self.neutral_us
        else:
            span = self.neutral_us - self.minimum_us
        pwm = self.neutral_us + int(round(signed * span))
        return max(self.minimum_us, min(self.maximum_us, pwm))


@dataclass(frozen=True, slots=True)
class ActuatorOutput:
    """A recorded software output; it is never sent to a physical device."""

    channel: int
    name: str
    semantic_command: float
    pwm_us: int
    source: str


@dataclass(frozen=True, slots=True)
class ActuatorSafetyConfig:
    """Validated channel map shared by all software and hardware adapters."""

    channels: tuple[ServoChannelSpec, ...]

    def __post_init__(self) -> None:
        if not self.channels:
            raise ActuatorSafetyError("actuator safety configuration needs at least one channel")
        names = [channel.name for channel in self.channels]
        numbers = [channel.channel for channel in self.channels]
        if len(names) != len(set(names)):
            raise ActuatorSafetyError("servo channel names must be unique")
        if len(numbers) != len(set(numbers)):
            raise ActuatorSafetyError("servo channel numbers must be unique")

    def outputs_for(self, commands: dict[str, float], *, source: str) -> tuple[ActuatorOutput, ...]:
        """Return one output per configured channel, rejecting missing/unknown names."""

        if not isinstance(commands, dict):
            raise ActuatorSafetyError("actuator commands must be a mapping")
        expected = {channel.name for channel in self.channels}
        supplied = set(commands)
        if supplied != expected:
            missing = sorted(expected - supplied)
            unknown = sorted(supplied - expected)
            raise ActuatorSafetyError(
                f"actuator command channels do not match configuration; missing={missing!r}, "
                f"unknown={unknown!r}"
            )
        return tuple(
            ActuatorOutput(
                channel=channel.channel,
                name=channel.name,
                semantic_command=float(commands[channel.name]),
                pwm_us=channel.pwm_for(commands[channel.name]),
                source=source,
            )
            for channel in self.channels
        )

    def neutral_outputs(self, *, source: str) -> tuple[ActuatorOutput, ...]:
        return self.outputs_for({channel.name: 0.0 for channel in self.channels}, source=source)


class ActuatorSafetySession:
    """State machine that makes cancel/loss/failsafe transitions one-way."""

    def __init__(self, config: ActuatorSafetyConfig) -> None:
        self.config = config
        self.state = ActuatorSafetyState.READY
        self.target_id: str | None = None
        self.entry_mode: str | None = None
        self.restored_mode: str | None = None
        self.outputs: list[ActuatorOutput] = []
        self.transitions: list[str] = [self.state.value]

    def bind(self, *, target_id: str, entry_mode: str) -> None:
        if not isinstance(target_id, str) or not target_id.strip():
            raise ActuatorSafetyError("target_id must be non-empty")
        if not isinstance(entry_mode, str) or not entry_mode.strip():
            raise ActuatorSafetyError("entry_mode must be non-empty")
        if self.state is ActuatorSafetyState.FAILSAFE:
            raise ActuatorSafetyError("failsafe session cannot be rebound")
        self.target_id = target_id
        self.entry_mode = entry_mode
        self.restored_mode = None
        self._set_state(ActuatorSafetyState.ACTIVE)

    def command(self, *, target_id: str, commands: dict[str, float]) -> tuple[ActuatorOutput, ...]:
        if self.state is not ActuatorSafetyState.ACTIVE:
            raise ActuatorSafetyError(f"actuator command rejected in {self.state.value} state")
        if target_id != self.target_id:
            raise ActuatorSafetyError("actuator command target binding does not match")
        outputs = self.config.outputs_for(commands, source="target")
        self.outputs.extend(outputs)
        return outputs

    def cancel(self, *, target_id: str) -> tuple[ActuatorOutput, ...]:
        self._require_active_target(target_id)
        outputs = self._emit_neutral("cancel")
        self.target_id = None
        self.entry_mode = None
        self.restored_mode = None
        self._set_state(ActuatorSafetyState.CANCELLED)
        return outputs

    def lost_lock(self, *, target_id: str) -> tuple[ActuatorOutput, ...]:
        self._require_active_target(target_id)
        outputs = self._emit_neutral("lost_lock")
        self.restored_mode = self.entry_mode
        self.target_id = None
        self.entry_mode = None
        self._set_state(ActuatorSafetyState.LOST_LOCK_RETURNED)
        return outputs

    def watchdog(self) -> tuple[ActuatorOutput, ...] | None:
        """Enter failsafe and emit exactly one canonical neutral frame."""

        if self.state is ActuatorSafetyState.FAILSAFE:
            return None
        outputs = self._emit_neutral("watchdog")
        self.target_id = None
        self.entry_mode = None
        self.restored_mode = None
        self._set_state(ActuatorSafetyState.FAILSAFE)
        return outputs

    def _require_active_target(self, target_id: str) -> None:
        if self.state is not ActuatorSafetyState.ACTIVE:
            raise ActuatorSafetyError(f"transition rejected in {self.state.value} state")
        if target_id != self.target_id:
            raise ActuatorSafetyError("transition target binding does not match")

    def _emit_neutral(self, source: str) -> tuple[ActuatorOutput, ...]:
        outputs = self.config.neutral_outputs(source=source)
        self.outputs.extend(outputs)
        return outputs

    def _set_state(self, state: ActuatorSafetyState) -> None:
        self.state = state
        self.transitions.append(state.value)


def _default_config() -> ActuatorSafetyConfig:
    return ActuatorSafetyConfig(
        channels=(
            ServoChannelSpec("roll_left", 1, 1100, 1500, 1900, direction=1),
            ServoChannelSpec("roll_right", 2, 1100, 1500, 1900, direction=-1),
            ServoChannelSpec("pitch", 3, 1200, 1500, 1800, direction=1),
        )
    )


def _pwm_map(outputs: tuple[ActuatorOutput, ...]) -> dict[str, int]:
    return {output.name: output.pwm_us for output in outputs}


def run_actuator_safety_software_acceptance() -> dict[str, Any]:
    """Run deterministic direction, limits, neutral and recovery checks."""

    config = _default_config()
    session = ActuatorSafetySession(config)
    session.bind(target_id="actuator-target-001", entry_mode="AUTO")
    active = session.command(
        target_id="actuator-target-001",
        commands={"roll_left": 0.75, "roll_right": 0.75, "pitch": -0.5},
    )
    active_pwm = _pwm_map(active)
    positive_left = config.channels[0].pwm_for(1.0)
    negative_right = config.channels[1].pwm_for(1.0)
    limit_high = config.channels[0].pwm_for(1.0)
    limit_low = config.channels[0].pwm_for(-1.0)
    direction_check = (
        active_pwm["roll_left"] > 1500
        and active_pwm["roll_right"] < 1500
        and positive_left == 1900
        and negative_right == 1100
    )
    limit_check = (
        limit_low == config.channels[0].minimum_us
        and limit_high == config.channels[0].maximum_us
        and config.channels[2].pwm_for(-1.0) == config.channels[2].minimum_us
        and config.channels[2].pwm_for(1.0) == config.channels[2].maximum_us
    )

    cancel_outputs = session.cancel(target_id="actuator-target-001")
    cancel_pwm = _pwm_map(cancel_outputs)
    neutral_values = {channel.name: channel.neutral_us for channel in config.channels}
    cancel_neutral = cancel_pwm == neutral_values
    cancel_clears_binding = (
        session.target_id is None and session.state is ActuatorSafetyState.CANCELLED
    )
    cancel_state = session.state.value

    session.bind(target_id="actuator-target-002", entry_mode="POSCTL")
    session.command(
        target_id="actuator-target-002",
        commands={"roll_left": -0.4, "roll_right": -0.4, "pitch": 0.2},
    )
    lost_outputs = session.lost_lock(target_id="actuator-target-002")
    lost_neutral = _pwm_map(lost_outputs) == neutral_values
    lost_lock_return = (
        lost_neutral
        and session.restored_mode == "POSCTL"
        and session.target_id is None
        and session.state is ActuatorSafetyState.LOST_LOCK_RETURNED
    )

    watchdog_session = ActuatorSafetySession(config)
    watchdog_session.bind(target_id="actuator-target-watchdog", entry_mode="AUTO")
    watchdog_session.command(
        target_id="actuator-target-watchdog",
        commands={"roll_left": 0.2, "roll_right": 0.2, "pitch": 0.0},
    )
    watchdog_outputs = watchdog_session.watchdog()
    watchdog_second = watchdog_session.watchdog()
    post_failsafe_error: str | None = None
    output_count_after_watchdog = len(watchdog_session.outputs)
    try:
        watchdog_session.command(
            target_id="actuator-target-watchdog",
            commands={"roll_left": 0.1, "roll_right": 0.1, "pitch": 0.1},
        )
    except ActuatorSafetyError as exc:
        post_failsafe_error = str(exc)
    no_post_failsafe_command = (
        post_failsafe_error is not None
        and len(watchdog_session.outputs) == output_count_after_watchdog
    )
    watchdog_neutral_once = (
        watchdog_outputs is not None
        and _pwm_map(watchdog_outputs) == neutral_values
        and watchdog_second is None
    )

    checks = {
        "direction_mapping": direction_check,
        "absolute_limits": limit_check,
        "cancel_emits_neutral": cancel_neutral,
        "cancel_clears_target_binding": cancel_clears_binding,
        "lost_lock_emits_neutral": lost_neutral,
        "lost_lock_restores_entry_mode": lost_lock_return,
        "watchdog_emits_neutral_once": watchdog_neutral_once,
        "no_command_after_failsafe": no_post_failsafe_command,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "event": EVENT,
        "runtime_mode": "HIL",
        "release_label": PRODUCT_VERSION.release_label,
        "gate_passed": all(checks.values()),
        "software_only": True,
        "physical_outputs_present": False,
        "propulsion_disconnected": "not_applicable_in_software_fixture",
        "checks": checks,
        "configuration": {
            "channels": [
                {
                    "name": channel.name,
                    "channel": channel.channel,
                    "minimum_us": channel.minimum_us,
                    "neutral_us": channel.neutral_us,
                    "maximum_us": channel.maximum_us,
                    "direction": channel.direction,
                }
                for channel in config.channels
            ],
            "command_domain": [-1.0, 1.0],
        },
        "active_command": {
            "roll_left": 0.75,
            "roll_right": 0.75,
            "pitch": -0.5,
            "pwm_us": active_pwm,
        },
        "cancel": {
            "state": cancel_state,
            "pwm_us": cancel_pwm,
            "target_cleared": cancel_clears_binding,
        },
        "lost_lock": {
            "state": session.state.value,
            "pwm_us": _pwm_map(lost_outputs),
            "restored_entry_mode": session.restored_mode,
            "target_cleared": session.target_id is None,
        },
        "watchdog": {
            "state": watchdog_session.state.value,
            "neutral_frame_count": 1 if watchdog_outputs is not None else 0,
            "second_watchdog_returned_none": watchdog_second is None,
            "post_failsafe_error": post_failsafe_error,
            "output_count_after_watchdog": output_count_after_watchdog,
        },
        "claims": {
            "hardware_acceptance": "not_claimed",
            "operator_visual_acceptance": "not_claimed",
            "real_actuator_acceptance": "not_claimed",
            "physical_output_enabled": False,
        },
    }


def _is_bool(value: object) -> bool:
    return type(value) is bool


def verify_actuator_safety_evidence(
    document: dict[str, Any], *, expected_release_label: str | None = None
) -> dict[str, Any]:
    """Recompute the software bench and reject altered evidence fields."""

    reasons: list[str] = []
    if not isinstance(document, dict):
        reasons.append("evidence must be an object")
        return {
            "schema_version": SCHEMA_VERSION,
            "event": VERIFIED_EVENT,
            "gate_passed": False,
            "checks": {},
            "reasons": reasons,
        }
    if document.get("schema_version") != SCHEMA_VERSION:
        reasons.append("schema_version must be 1")
    if document.get("event") != EVENT:
        reasons.append(f"event must be {EVENT!r}")
    if document.get("runtime_mode") != "HIL":
        reasons.append("runtime_mode must be HIL")
    release_label = expected_release_label or PRODUCT_VERSION.release_label
    if document.get("release_label") != release_label:
        reasons.append("release_label does not match the current product manifest")
    if document.get("software_only") is not True:
        reasons.append("software_only must be true")
    if document.get("physical_outputs_present") is not False:
        reasons.append("physical_outputs_present must be false")
    if document.get("propulsion_disconnected") != "not_applicable_in_software_fixture":
        reasons.append("propulsion_disconnected must identify the software fixture")
    claims = document.get("claims")
    if not isinstance(claims, dict):
        reasons.append("claims must be an object")
    else:
        for key in (
            "hardware_acceptance",
            "operator_visual_acceptance",
            "real_actuator_acceptance",
        ):
            if claims.get(key) != "not_claimed":
                reasons.append(f"claims.{key} must remain not_claimed")
        if claims.get("physical_output_enabled") is not False:
            reasons.append("claims.physical_output_enabled must be false")

    canonical = run_actuator_safety_software_acceptance()
    expected_checks = canonical["checks"]
    declared_checks = document.get("checks")
    if not isinstance(declared_checks, dict) or set(declared_checks) != set(expected_checks):
        reasons.append("checks must contain exactly the canonical check keys")
        checks = {key: False for key in expected_checks}
    else:
        checks = {}
        for key, expected in expected_checks.items():
            value = declared_checks.get(key)
            if not _is_bool(value):
                reasons.append(f"checks.{key} must be a boolean")
                checks[key] = False
            else:
                checks[key] = value is expected
                if value is not expected:
                    reasons.append(f"checks.{key} does not match the canonical result")

    # The deterministic sections are compared after allowing the caller to
    # carry a generated timestamp in a wrapper script.  This catches a copied
    # ``gate_passed`` bit or modified PWM evidence, not just the check map.
    for section in ("configuration", "active_command", "cancel", "lost_lock", "watchdog"):
        if document.get(section) != canonical.get(section):
            reasons.append(f"{section} does not match the canonical software bench")
    declared_gate = document.get("gate_passed")
    expected_gate = not reasons and all(checks.values())
    if not _is_bool(declared_gate):
        reasons.append("gate_passed must be a boolean")
    elif declared_gate is not expected_gate:
        reasons.append("gate_passed does not match the recomputed result")
    return {
        "schema_version": SCHEMA_VERSION,
        "event": VERIFIED_EVENT,
        "runtime_mode": "HIL",
        "release_label": document.get("release_label"),
        "gate_passed": not reasons and all(checks.values()),
        "checks": checks,
        "reasons": reasons,
    }


__all__ = [
    "ActuatorOutput",
    "ActuatorSafetyConfig",
    "ActuatorSafetyError",
    "ActuatorSafetySession",
    "ActuatorSafetyState",
    "EVENT",
    "SCHEMA_VERSION",
    "ServoChannelSpec",
    "VERIFIED_EVENT",
    "run_actuator_safety_software_acceptance",
    "verify_actuator_safety_evidence",
]
