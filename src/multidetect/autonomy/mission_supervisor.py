"""Mission-level policy above the executor; communication is always optional."""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from multidetect.sim_teacher.primitives import ActionPrimitive, body_to_ned

from .energy_estimator import EnergyConfig, OnboardEnergyEstimator
from .mission_executor import MissionExecutor
from .mission_package import POLICIES, MissionPackage, check_plan_against_package, number, parse_policy
from .mission_plan import validate_mission_plan
from .mission_security import load_key, verify_document
from .mission_summary import summarize_mission
from .onboard_mission_log import DownlinkQueue, OnboardMissionLog
from .serialization import strict_json_loads
from .crash_resume import CheckpointStore, checkpoint_from_state

PLAN_COMMANDS = frozenset({"approve_plan", "reject_plan", "approve_retask", "reject_retask"})
# Fixed-layout TUNNEL controls carry no document signature; the operator
# tunnel's HMAC, MAVLink signing and sequence high-water authenticate them.
# Plan decisions never take this path: they must be signed documents.
TRANSPORT_AUTHENTICATED_EVENTS = frozenset(
    {"heartbeat", "abort", "pause", "resume", "policy_update"}
)


def plan_digest(package: MissionPackage, plan: Any) -> str:
    """Approval digest binding the mission id, the admitted package and its plan.

    An approval for one mission's plan cannot approve the same steps under a
    different package (other zones, budget, home or refs) or mission id.
    """

    def canonical(value: Any) -> bytes:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=list,
        ).encode("utf-8")

    return hashlib.sha256(
        canonical(
            {
                "v": 2,
                "mission_id": package.mission_id,
                "package_sha256": hashlib.sha256(canonical(package.document)).hexdigest(),
                "refs_sha256": hashlib.sha256(canonical(package.refs)).hexdigest(),
                "plan": asdict(plan),
            }
        )
    ).hexdigest()


def count_lower_bounds(executor: Any) -> dict[str, bool]:
    """Per count step: True when the count may have missed objects.

    A count step that has not finished (the mission ended early, or is still
    searching) is always a lower bound; a finished one keeps the executor's
    coverage verdict.
    """
    finished: dict[str, bool] = {}
    for item in getattr(executor, "history", ()):
        if isinstance(item, dict) and "count_is_lower_bound" in item:
            finished[str(item.get("step"))] = bool(item["count_is_lower_bound"])
    return {str(step): finished.get(str(step), True) for step in executor.counts}


@dataclass(frozen=True, slots=True)
class RecoveryPolicy:
    """Deterministic recovery action vocabulary for each airframe category."""

    category: str = "multirotor"
    landing_method: str = "runway"
    transition_altitude_m: float = 30.0
    transition_min_airspeed_mps: float = 12.0
    parachute_available: bool = False

    def __post_init__(self) -> None:
        if self.category not in {"multirotor", "fixed_wing", "vtol"}:
            raise ValueError("category must be multirotor, fixed_wing or vtol")
        if self.landing_method not in {"runway", "belly", "parachute"}:
            raise ValueError("landing_method must be runway, belly or parachute")
        if self.transition_altitude_m < 0 or self.transition_min_airspeed_mps < 0:
            raise ValueError("transition limits must be non-negative")

    def sequence(
        self, *, trigger: str = "return", energy_available: bool = True, mode: str | None = None
    ) -> tuple[str, ...]:
        if self.category == "multirotor":
            return (
                ("land_now",)
                if not energy_available
                else (("hold", "return_home") if trigger == "link_loss" else ("return_home",))
            )
        if self.category == "fixed_wing":
            if not energy_available and self.parachute_available:
                return ("parachute",)
            landing = (
                "parachute"
                if self.landing_method == "parachute" and self.parachute_available
                else "fw_land"
            )
            return ("loiter", "return_home", landing)
        if not energy_available:
            return ("parachute",) if self.parachute_available else ("fw_land",)
        return ("loiter", "return_home", "vtol_transition_to_mc", "land_now")

    def first_action(
        self, *, trigger: str = "return", energy_available: bool = True, mode: str | None = None
    ) -> str:
        return self.sequence(trigger=trigger, energy_available=energy_available, mode=mode)[0]


class MissionSupervisor:
    """Wrap a validated executor without changing its step graph or safety layer.

    Return and land decisions latch. A reconnect can release a policy hold but
    cannot resume an abandoned mission. Updates can only tighten the policy or
    its existing deadline. Event timestamps use the host clock, not remote time.
    """

    def __init__(
        self,
        executor: MissionExecutor,
        package: MissionPackage,
        *,
        log: OnboardMissionLog,
        downlink: DownlinkQueue | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        security_key: bytes | None = None,
        security_key_path: str | None = None,
        timestamp_window_s: float = 30.0,
        clock_skew_s: float = 5.0,
        rate_limit: int = 20,
        rate_window_s: float = 10.0,
        energy_config: EnergyConfig | None = None,
        recovery_policy: RecoveryPolicy | None = None,
        replay_state_path: str | Path | None = None,
        max_sequence_jump: int = 1024,
        runtime_mode: str | None = None,
        flight_mode: bool | None = None,
        require_signature: bool | None = None,
        ground_command_verify_key: bytes | None = None,
        requires_approval: bool | None = None,
        checkpoint_path: str | Path | None = None,
    ) -> None:
        package.validate_plan(executor.plan)
        self.executor, self.package, self.log, self.downlink = executor, package, log, downlink
        self.clock = clock
        self.wall_clock = wall_clock
        if security_key is None and security_key_path is not None:
            security_key = load_key(security_key_path)
        self._security_key = ground_command_verify_key or security_key
        self._ground_lock = threading.RLock()
        self._auth_failures: deque[float] = deque()
        mode_text = runtime_mode.strip().lower() if isinstance(runtime_mode, str) else ""
        self._flight_mode = (
            bool(flight_mode)
            if flight_mode is not None
            else mode_text in {"flight", "real", "hil", "live"}
        )
        self._require_signature = (
            bool(require_signature)
            if require_signature is not None
            else self._flight_mode or security_key is not None
        )
        if self._flight_mode and self._security_key is None:
            raise ValueError("flight mission requires an authentication key")
        self.timestamp_window_s = number(timestamp_window_s, "timestamp_window_s", minimum=0.001)
        self.clock_skew_s = number(clock_skew_s, "clock_skew_s")
        if isinstance(rate_limit, bool) or not isinstance(rate_limit, int) or rate_limit < 1:
            raise ValueError("rate_limit must be a positive integer")
        self.rate_limit, self.rate_window_s = (
            rate_limit,
            number(rate_window_s, "rate_window_s", minimum=0.001),
        )
        self._event_times: deque[float] = deque()
        self._last_sequence = 0
        if (
            isinstance(max_sequence_jump, bool)
            or not isinstance(max_sequence_jump, int)
            or max_sequence_jump < 1
            or max_sequence_jump >= 2**31
        ):
            raise ValueError("max_sequence_jump must be a positive serial-number distance")
        self.max_sequence_jump = max_sequence_jump
        self._sequence_modulus = 2**32
        self._replay_state_path = Path(replay_state_path) if replay_state_path is not None else None
        self._replay_state_loaded = False
        self._replay_state_error: str | None = None
        self._last_remote_heartbeat: float | None = None
        self._last_remote_timestamp_domain: str | None = None
        self.started = self._now = clock()
        if not math.isfinite(self.started):
            raise ValueError("invalid supervisor clock")
        self.last_heartbeat: float | None = None
        self.policy, self.timeout_s = parse_policy(package.document["link_loss_policy"])
        self.paused_at: float | None = None
        self.action, self.reason = "continue", "mission admitted; onboard operation enabled"
        self._terminal: str | None = None
        self._recovery: MissionExecutor | None = None
        self._recovery_kind: str | None = None
        self.recovery_policy = recovery_policy
        self.recovery_sequence: tuple[str, ...] = ()
        self.recovery_action: str = "continue"
        self._brake_pending = False
        self._low_confidence = 0
        self._remaining_time: float | None = None
        self._time_estimated_at = self.started
        self._remaining_energy: float | None = None
        self._finished = False
        self._finish_lock = threading.Lock()
        self.requires_approval = bool(requires_approval)
        self.plan_sha256 = plan_digest(package, executor.plan)
        self.package_digest = hashlib.sha256(
            json.dumps(package.document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        self.checkpoint_store = CheckpointStore(checkpoint_path) if checkpoint_path is not None else None
        self._checkpoint_sequence = 0
        self.plan_approved = not self.requires_approval
        self.plan_rejected = False
        self._last_envelope_violation: tuple[str, str] | None = None
        self._last_envelope_violation_event_s: float | None = None
        self.plan_version = 1
        self.plan_history: list[dict[str, Any]] = [{"version": 1, "mode": "initial", "plan_sha256": self.plan_sha256}]
        self._pending_retask_approval: tuple[Any, ...] | None = None
        self._retask_lock = threading.RLock()
        self._retask_planning = False
        # An existing mission log means this is a restart.  A missing replay
        # file in that case is evidence loss and must fail closed.
        self._had_prior_log = self.log.path.exists()
        self.energy_estimator = OnboardEnergyEstimator(
            EnergyConfig.from_budget(
                package.document["budget"], asdict(energy_config) if energy_config else None
            )
        )
        if self.recovery_policy is None:
            self.recovery_policy = RecoveryPolicy(category=self.energy_estimator.params.category)
        self.energy_estimate: dict[str, Any] | None = None
        executor.home = tuple(package.document["home"])
        if executor.airframe_adapter is not None:
            executor.airframe_adapter.safety.set_home(executor.home)
        energy_model = asdict(self.energy_estimator.config)
        if self.energy_estimator.params.category == "multirotor":
            # Preserve the pre-profile mission log shape for the default path.
            energy_model.pop("vehicle", None)
        self._record(
            "mission_started",
            package=package.document,
            plan=asdict(executor.plan),
            energy_model=energy_model,
        )
        self._ensure_replay_state(admission=True)

    def _retask_envelope_ok(self, candidate: MissionPackage) -> tuple[bool, str]:
        old, new = self.package.document, candidate.document
        oa, na = old["operating_area"], candidate.document["operating_area"]
        if any(float(na[k]) < float(oa[k]) for k in ("north_min_m", "east_min_m", "min_altitude_m")) or any(
            float(na[k]) > float(oa[k]) for k in ("north_max_m", "east_max_m", "max_altitude_m")
        ):
            return False, "改派区域或高度扩大了原任务包络"
        if not set(new.get("zones", {})) <= set(old.get("zones", {})):
            return False, "改派引用了原任务包络外区域"
        return True, "改派包络校验通过"

    def request_retask(self, candidate, *, mode="replace", planner=None, timeout_s=30.0, requires_approval=False, approval_timeout_s=600.0):
        if mode not in {"append", "replace"}:
            raise ValueError("retask mode must be append or replace")
        self._record("retask_received", mode=mode)
        self._retask_planning = True
        result = {}
        def work():
            try:
                result["candidate"] = planner() if planner else candidate
            except Exception as exc:
                result["error"] = exc
        thread = threading.Thread(target=work, name="mission-retask-planner", daemon=True)
        thread.start(); thread.join(max(0.001, float(timeout_s)))
        self._retask_planning = False
        if thread.is_alive():
            self._record("retask_planning", status="timeout", reason="改派规划超时，保持原计划")
            self._record("retask_rejected", mode=mode, reason="改派规划超时，保持原计划")
            return False
        if "error" in result:
            self._record("retask_rejected", mode=mode, reason=f"改派规划失败：{result['error']}")
            return False
        value = result.get("candidate")
        package = getattr(value, "package", value if isinstance(value, MissionPackage) else None)
        plan = getattr(getattr(value, "executor", None), "plan", getattr(value, "plan", None))
        try:
            if not isinstance(package, MissionPackage) or plan is None:
                raise ValueError("改派缺少有效签名任务包或计划")
            package.validate_plan(plan)
            violations = check_plan_against_package(plan, package, executor_config=self.executor.config)
            if violations:
                raise ValueError(violations[0].get("message_zh", "改派计划校验失败"))
            ok, reason = self._retask_envelope_ok(package)
            if not ok:
                raise ValueError(reason)
            budget = package.document["budget"]
            if self._remaining_energy is not None and self._remaining_energy < budget["energy_wh"] + budget["reserve_energy_wh"]:
                raise ValueError("剩余能量不足以完成改派并返航，拒绝改派")
            if self._remaining_time is not None and self._remaining_time < budget["time_s"] + budget["reserve_time_s"]:
                raise ValueError("剩余时间不足以完成改派并返航，拒绝改派")
            self._record("retask_validated", mode=mode, status="accepted", reason="计划、包络及预算校验通过")
            version = self.plan_version + 1
            digest = plan_digest(package, plan)
            if requires_approval or getattr(value, "requires_approval", False):
                self._pending_retask_approval = (plan, package, mode, version, digest)
                self._record("retask_approval", status="awaiting", plan_version=version, plan_sha256=digest)
                def expire() -> None:
                    with self._ground_lock:
                        if self._pending_retask_approval and self._pending_retask_approval[4] == digest:
                            self._pending_retask_approval = None
                            self._record("retask_rejected", reason="改派审批超时", plan_version=version)
                timer = threading.Timer(max(0.001, float(approval_timeout_s)), expire)
                timer.daemon = True
                timer.start()
                return True
            self.executor.request_retask(plan, mode=mode, version=version)
            self.plan_version = version
            self.plan_history.append({"version": version, "mode": mode, "plan_sha256": digest})
            self._record("retask_switched", mode=mode, plan_version=version, plan_sha256=digest)
            return True
        except Exception as exc:
            self._record("retask_rejected", mode=mode, reason=str(exc))
            return False

    def __getattr__(self, name):
        return getattr(self.executor, name)

    @property
    def session_nonce(self) -> str:
        """Deprecated compatibility accessor; nonce is no longer security state."""
        return ""

    @property
    def done(self) -> bool:
        if self._terminal is not None:
            skill = "land" if self._terminal == "land_now" else "return_home"
            if self._recovery_kind != skill or self._brake_pending:
                return False
        return self._recovery.done if self._recovery is not None else self.executor.done

    @property
    def connected(self) -> bool:
        return (
            self.last_heartbeat is not None
            and self._now - self.last_heartbeat < self.package.document["heartbeat_timeout_s"]
        )

    @staticmethod
    def _atomic_json(path: Path, document: dict[str, Any]) -> None:
        """Write replay state so a torn write cannot reset the high-water mark."""
        path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(path.parent, 0o700)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        encoded = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
        try:
            with temporary.open("xb") as handle:
                if os.name != "nt":
                    os.chmod(temporary, 0o600)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                # Directory fsync is unavailable on a few filesystems; replace is still atomic.
                pass
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _replay_path(self) -> Path:
        if self._replay_state_path is None:
            self._replay_state_path = self.log.directory / "ground-replay-state.json"
        return self._replay_state_path

    def _ensure_replay_state(self, *, admission: bool = False) -> bool:
        """Load mission-scoped high-water state; create only on first admission."""
        if self._replay_state_loaded:
            return self._replay_state_error is None
        path = self._replay_path()
        try:
            if path.parent.is_symlink():
                raise ValueError("replay state directory symlink is refused")
            if path.is_symlink():
                raise ValueError("replay state symlink is refused")
            if os.name != "nt":
                parent_stat = path.parent.stat()
                if parent_stat.st_uid != os.getuid():
                    raise PermissionError("replay state owner mismatch")
                if path.exists():
                    state_stat = path.stat()
                    if state_stat.st_uid != os.getuid():
                        raise PermissionError("replay state owner mismatch")
                    if state_stat.st_mode & 0o077:
                        raise PermissionError("replay state permissions are too broad")
            prior_sequence = self._last_sequence
            had_prior_state = path.exists()
            if had_prior_state:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("replay state is invalid")
                if raw.get("version") not in {1, 2}:
                    raise ValueError("replay state version is invalid")
                if raw.get("version") == 2:
                    prior_sequence = raw.get("missions", {}).get(self.package.mission_id)
                    if prior_sequence is None:
                        prior_sequence = 0
                else:
                    if raw.get("mission_id") != self.package.mission_id:
                        raise ValueError("replay state mission mismatch")
                    prior_sequence = raw.get("last_sequence")
                if (
                    isinstance(prior_sequence, bool)
                    or not isinstance(prior_sequence, int)
                    or not 0 <= prior_sequence < self._sequence_modulus
                ):
                    raise ValueError("replay state sequence is invalid")
            elif self._had_prior_log:
                raise ValueError("replay state lost; re-provision mission")
            self._last_sequence = prior_sequence
            if not had_prior_state:
                self._atomic_json(path, {"version": 2, "missions": {self.package.mission_id: 0}})
            self._replay_state_loaded = True
            return True
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self._replay_state_error = str(exc)
            self._replay_state_loaded = True
            if admission:
                self._record(
                    "replay_state_lost",
                    status="replay state lost; re-provision mission",
                    error=str(exc),
                )
            return False

    def _commit_sequence(self, sequence: int) -> None:
        if not self._ensure_replay_state():
            raise ValueError("replay state unavailable")
        path = self._replay_path()
        missions = {}
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            missions = dict(raw.get("missions", {})) if raw.get("version") == 2 else {}
        missions[self.package.mission_id] = sequence
        state = {"version": 2, "missions": missions}
        self._atomic_json(path, state)
        self._last_sequence = sequence

    def _serial_distance(self, sequence: int) -> int:
        return (sequence - self._last_sequence) & (self._sequence_modulus - 1)

    def _record(self, kind: str, **payload) -> None:
        self.log.append(kind, self._now, **payload)
        if self.downlink is not None:
            self.downlink.notify()

    def restore_checkpoint(self, checkpoint: Mapping[str, Any]) -> None:
        """Restore bounded mission state after admission; caller made the safety decision."""
        if checkpoint.get("mission_id") != self.package.mission_id or checkpoint.get("package_digest") != self.package_digest:
            raise ValueError("checkpoint identity does not match admitted mission")
        step_id = checkpoint.get("step_id")
        step = next((item for item in self.executor.plan.steps if item.id == step_id), None)
        if step is None:
            raise ValueError("checkpoint step is not in admitted plan")
        self.executor._run = self.executor._start(step)
        self.executor.counts = dict(checkpoint.get("counts", {}))
        self.executor.alerts = list(checkpoint.get("alerts", []))
        self.executor.history = list(checkpoint.get("history", []))
        self.executor.findings = list(checkpoint.get("findings", []))
        if getattr(self.executor, "_run", None) is not None and checkpoint.get("phase"):
            self.executor._run.phase = str(checkpoint["phase"])
        self._checkpoint_sequence = int(checkpoint.get("sequence", 0))
        self._record("crash_resume", action="resume", reason_zh="从有效检查点恢复", step_id=step_id)

    def _time(self) -> None:
        now = self.clock()
        if not math.isfinite(now) or now < self._now:
            raise ValueError("invalid supervisor clock")
        self._now = now

    def ground_event(self, event: dict[str, Any], *, transport_authenticated: bool = False) -> bool:
        """Apply one ground command; the only path that changes operator state.

        ``transport_authenticated`` is set only by the operator TUNNEL adapter
        for its fixed-layout controls (heartbeat, abort, pause, resume, policy
        tightening).  Those frames are already authenticated and replay-checked
        by the tunnel, so they need no document signature.  Plan approval and
        rejection always need the document signature when a key is configured.
        """
        with self._ground_lock:
            return self._ground_event_locked(event, transport_authenticated=transport_authenticated)

    def _ground_event_locked(
        self, event: dict[str, Any], *, transport_authenticated: bool = False
    ) -> bool:
        self._time()
        raw_input = event
        if isinstance(event, (str, bytes, bytearray)):
            if len(raw_input) > 8192:
                self._record(
                    "ground_event_rejected",
                    reason="pre-authentication byte limit exceeded",
                )
                return False
            try:
                event = strict_json_loads(event, name="ground command")
            except (TypeError, ValueError):
                return False
        if not isinstance(event, dict):
            self._record("ground_event_rejected", reason="event must be an object")
            return False
        transport_only = (
            transport_authenticated and event.get("type") in TRANSPORT_AUTHENTICATED_EVENTS
        )
        secured = self._security_key is not None and not transport_only
        raw_size = len(json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode())

        def depth(value, level=0):
            if level > 8:
                return level
            if isinstance(value, dict):
                return max([level, *(depth(v, level + 1) for v in value.values())])
            if isinstance(value, list):
                return max([level, *(depth(v, level + 1) for v in value)])
            return level

        def strings_ok(value, key=None):
            if isinstance(value, str):
                limit = 128 if key in {"signature", "mac"} else 1024
                return len(value) <= limit
            if isinstance(value, dict):
                return all(strings_ok(v, k) for k, v in value.items())
            if isinstance(value, list):
                return all(strings_ok(v, key) for v in value)
            return True

        if raw_size > 8192 or depth(event) > 8 or not strings_ok(event):
            self._record("ground_event_rejected", reason="pre-authentication limits exceeded")
            return False
        while self._auth_failures and self._now - self._auth_failures[0] > self.rate_window_s:
            self._auth_failures.popleft()
        if self._require_signature and not secured and not transport_only:
            self._record("ground_event_rejected", reason="flight control requires authentication")
            return False
        sequence: int | None = None
        timestamp_domain = "monotonic"
        stamp_number: float
        if secured:
            sequence = event.get("sequence")
            signature = event.get("signature", event.get("mac"))
            if not isinstance(signature, str) or not verify_document(event, self._security_key):
                self._auth_failures.append(self._now)
                failures = len(self._auth_failures)
                # Log every failure up to the threshold, then only every 100th, so a
                # flood of forged commands cannot fill the onboard disk with log lines.
                if failures <= 20 or failures % 100 == 0:
                    self._record(
                        "ground_event_rejected",
                        reason=(
                            "event authentication failed; failure threshold exceeded"
                            if failures > 20
                            else "event authentication failed"
                        ),
                        failures_in_window=failures,
                    )
                return False
            try:
                stamp_number = number(event.get("timestamp"), "event timestamp")
            except (ValueError, TypeError) as exc:
                self._record("ground_event_rejected", reason=str(exc))
                return False
            if not self._ensure_replay_state():
                self._record("ground_event_rejected", reason="replay state unavailable")
                return False
            if (
                isinstance(sequence, bool)
                or not isinstance(sequence, int)
                or not 0 <= sequence < self._sequence_modulus
            ):
                self._record("ground_event_rejected", reason="sequence invalid")
                return False
            distance = self._serial_distance(sequence)
            if distance == 0 or distance > self.max_sequence_jump:
                self._record("ground_event_rejected", reason="sequence replay or jump too large")
                return False
            timestamp_domain = event.get("timestamp_domain")
            if event.get("timestamp_version") != 1:
                self._record("ground_event_rejected", reason="unsupported timestamp version")
                return False
            if timestamp_domain is not None and timestamp_domain not in {"monotonic", "unix"}:
                self._record("ground_event_rejected", reason="unsupported timestamp domain")
                return False
            if timestamp_domain is None:
                self._record("ground_event_rejected", reason="timestamp domain is required")
                return False
            try:
                if timestamp_domain == "unix":
                    wall = self.wall_clock()
                    if not math.isfinite(wall):
                        raise ValueError("invalid local clock")
                    reference = wall
                else:
                    reference = self._now
                if abs(stamp_number - reference) > self.timestamp_window_s + self.clock_skew_s:
                    raise ValueError("stale event timestamp")
            except (ValueError, TypeError) as exc:
                self._record("ground_event_rejected", reason=str(exc))
                return False
        else:
            # Unsigned SIM/test events have no remote clock; receipt time is the
            # only timestamp that can safely be used for liveness and ordering.
            try:
                stamp_number = number(event.get("timestamp", self._now), "event timestamp")
            except (ValueError, TypeError) as exc:
                self._record("ground_event_rejected", reason=str(exc))
                return False
        if event.get("mission_id") != self.package.mission_id:
            self._record("ground_event_rejected", reason="mission id mismatch")
            return False
        kind = event.get("type")
        if kind not in TRANSPORT_AUTHENTICATED_EVENTS | PLAN_COMMANDS:
            self._record("ground_event_rejected", reason="unknown event type")
            return False
        if not secured and not self.started <= stamp_number <= self._now:
            self._record("ground_event_rejected", reason="event timestamp outside this flight")
            return False
        # Rate-limit before invoking any state-changing operation.  This gate
        # intentionally applies to unsigned SIM/test traffic as well.
        while self._event_times and self._now - self._event_times[0] >= self.rate_window_s:
            self._event_times.popleft()
        if len(self._event_times) >= self.rate_limit:
            self._record("ground_event_rejected", reason="event rate limit exceeded")
            return False

        previous = {
            "policy": self.policy,
            "timeout_s": self.timeout_s,
            "last_heartbeat": self.last_heartbeat,
            "last_remote_heartbeat": self._last_remote_heartbeat,
            "last_remote_timestamp_domain": self._last_remote_timestamp_domain,
            "paused_at": self.paused_at,
            "terminal": self._terminal,
            "finished": self._finished,
            "brake_pending": self._brake_pending,
            "action": self.action,
            "reason": self.reason,
        }
        try:
            if kind in PLAN_COMMANDS:
                digest = event.get("plan_sha256")
                expected = self._pending_retask_approval[4] if kind in {"approve_retask", "reject_retask"} and self._pending_retask_approval else self.plan_sha256
                if not isinstance(digest, str) or digest != expected:
                    raise ValueError("plan digest mismatch or stale")
                if self.plan_rejected or (kind == "reject_plan" and self.plan_approved):
                    raise ValueError("plan decision already made")
            elif kind == "policy_update":
                policy, timeout = parse_policy(event.get("policy"))
                old_rank, new_rank = POLICIES.index(self.policy), POLICIES.index(policy)
                timed = policy in {"continue_then_return", "hold_then_return"}
                if new_rank < old_rank or (
                    new_rank == old_rank and timed and timeout > self.timeout_s
                ):
                    raise ValueError("policy update would relax operator policy")
                if timed and self.policy in {"continue_then_return", "hold_then_return"}:
                    # A stronger hold policy must not extend the old return deadline.
                    old_deadline = self.timeout_s + (
                        self.package.document["heartbeat_timeout_s"]
                        if self.policy == "hold_then_return"
                        else 0
                    )
                    offset = (
                        self.package.document["heartbeat_timeout_s"]
                        if policy == "hold_then_return"
                        else 0
                    )
                    if old_deadline < offset:
                        raise ValueError("policy update would extend the existing return deadline")
                    timeout = max(0.0, min(timeout, old_deadline - offset))
                self.policy, self.timeout_s = policy, timeout
            elif kind == "heartbeat":
                if (
                    self._last_remote_heartbeat is not None
                    and timestamp_domain == self._last_remote_timestamp_domain
                    and stamp_number < self._last_remote_heartbeat
                ):
                    raise ValueError("stale heartbeat")
                # Liveness is always measured in the local monotonic domain.
                self.last_heartbeat = self._now
                self._last_remote_heartbeat = stamp_number
                self._last_remote_timestamp_domain = timestamp_domain
            elif kind == "abort":
                self._escalate(
                    "return_now",
                    "explicit operator abort",
                    record=not secured,
                )
            elif kind == "pause":
                if self.paused_at is None:
                    self.paused_at = self._now
            elif kind == "resume":
                self.paused_at = None
        except (ValueError, TypeError, KeyError) as exc:
            self._record("ground_event_rejected", reason=str(exc), event_type=kind)
            return False
        if secured:
            assert sequence is not None
            try:
                self._commit_sequence(sequence)
            except (OSError, ValueError, TypeError) as exc:
                # Keep state unchanged when durable replay protection cannot
                # commit.  A failure must never turn into an accepted command.
                self.policy = previous["policy"]
                self.timeout_s = previous["timeout_s"]
                self.last_heartbeat = previous["last_heartbeat"]
                self._last_remote_heartbeat = previous["last_remote_heartbeat"]
                self._last_remote_timestamp_domain = previous["last_remote_timestamp_domain"]
                self.paused_at = previous["paused_at"]
                self._terminal = previous["terminal"]
                self._finished = previous["finished"]
                self._brake_pending = previous["brake_pending"]
                self.action, self.reason = previous["action"], previous["reason"]
                self._record("ground_event_rejected", reason=f"replay state commit failed: {exc}")
                return False
            if self._terminal != previous["terminal"]:
                self._record(
                    "escalation",
                    action=self._terminal,
                    reason=self.reason,
                    operator_policy=self.policy,
                    energy=self.energy_estimate,
                )
        self._event_times.append(self._now)
        self._record(
            "ground_event",
            event_type=kind,
            received_timestamp=stamp_number,
            timestamp_domain=timestamp_domain,
            timestamp_version=1,
            policy=self.policy,
            timeout_s=self.timeout_s,
        )
        if kind == "approve_plan":
            self.plan_approved = True
            self._record("plan_approved", plan_sha256=self.plan_sha256)
        elif kind in {"approve_retask", "reject_retask"}:
            pending = self._pending_retask_approval
            if pending is None:
                self._record("retask_rejected", reason="无待审批改派")
                return False
            plan, _package, mode, version, digest = pending
            self._pending_retask_approval = None
            if kind == "approve_retask":
                self.executor.request_retask(plan, mode=mode, version=version)
                self.plan_version = version
                self.plan_history.append({"version": version, "mode": mode, "plan_sha256": digest})
                self._record("retask_switched", mode=mode, plan_version=version, plan_sha256=digest)
                self._record("retask_approval", status="approved", plan_version=version)
            else:
                self._record("retask_rejected", reason="操作员拒绝改派", plan_version=version)
        elif kind == "reject_plan":
            # A rejected plan never runs: latch land_now so the host lands (or
            # stays on the ground) and the mission reaches a terminal state.
            self.plan_rejected = True
            self._escalate("land_now", "operator rejected mission plan")
            self.action, self.reason = "land_now", "operator rejected mission plan"
            self._record("plan_rejected", plan_sha256=self.plan_sha256)
            self.finish("plan_rejected")
        if self.downlink is not None:
            self.downlink.set_connected(self.connected)
        return True

    def update_estimates(
        self, *, remaining_time_s=None, remaining_energy_wh=None, low_confidence_count=None
    ) -> None:
        self._time()
        if remaining_time_s is not None:
            self._remaining_time = number(remaining_time_s, "remaining_time_s")
            self._time_estimated_at = self._now
        if remaining_energy_wh is not None:
            self._remaining_energy = number(remaining_energy_wh, "remaining_energy_wh")
        if low_confidence_count is not None:
            if (
                isinstance(low_confidence_count, bool)
                or not isinstance(low_confidence_count, int)
                or low_confidence_count < 0
            ):
                raise ValueError("low_confidence_count must be a nonnegative integer")
            self._low_confidence = low_confidence_count
        self._record(
            "estimates",
            remaining_time_s=self._remaining_time,
            remaining_energy_wh=self._remaining_energy,
            low_confidence_count=self._low_confidence,
        )

    def ingest_energy_telemetry(self, message, *, received_at_s=None) -> bool:
        """Onboard receive hook; cached messages must retain their receipt time."""
        self._time()
        stamp = self._now if received_at_s is None else number(received_at_s, "receipt time")
        if stamp > self._now:
            raise ValueError("energy telemetry receipt is in the future")
        return self.energy_estimator.ingest(message, received_at_s=stamp)

    def _escalate(self, action: str, reason: str, *, record: bool = True) -> None:
        if self._terminal is None or POLICIES.index(action) > POLICIES.index(self._terminal):
            self._terminal = action
            self._brake_pending = action == "return_now"
            self.reason = reason
            self.recovery_sequence = self.recovery_policy.sequence(
                trigger="link_loss" if "link" in reason else "return",
                energy_available=action != "land_now",
            )
            self.recovery_action = self.recovery_sequence[0] if self.recovery_sequence else action
            if record:
                self._record(
                    "escalation",
                    action=action,
                    reason=reason,
                    operator_policy=self.policy,
                    energy=self.energy_estimate,
                )

    def report_envelope_violation(self, reason: str, *, action: str = "return_now") -> str:
        """Report a final-action envelope veto to the supervisor.

        The safety envelope is deliberately a stateless arbiter with respect to
        mission execution: it may hold or clip a primitive, but it must not
        mutate an executor or recovery state itself.  Callers therefore route a
        veto through this method.  The supervisor owns escalation and applies
        its existing policy ordering (``land_now`` always dominates
        ``return_now``).

        ``action`` is restricted to terminal actions.  A successful report
        returns the currently latched supervisor action, which is useful to
        callers that need to annotate the outgoing hold primitive.
        """
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("envelope violation reason must be a non-empty string")
        if action not in {"return_now", "land_now"}:
            raise ValueError("envelope violation action must be return_now or land_now")
        clean_reason = reason.strip()
        with self._ground_lock:
            self._time()
            key = (clean_reason, action)
            should_record = (
                key != self._last_envelope_violation
                or self._last_envelope_violation_event_s is None
                or self._now - self._last_envelope_violation_event_s >= 5.0
            )
            if should_record:
                self._last_envelope_violation = key
                self._last_envelope_violation_event_s = self._now
        message = f"safety envelope violation: {clean_reason}"
        # _escalate is the sole owner of terminal/recovery state and preserves
        # the supervisor's established precedence when a stronger action was
        # already latched (in particular, land_now cannot be downgraded).
        self._escalate(action, message)
        if should_record:
            self._record(
                "envelope_violation",
                requested_action=action,
                effective_action=self._terminal or self.action,
                reason=clean_reason,
            )
        return self._terminal or self.action

    def evaluate(self, *, pose_ned=None, proposed_action=None, yaw_deg=0.0) -> str:
        self._time()
        doc = self.package.document
        silence = self._now - (
            self.last_heartbeat if self.last_heartbeat is not None else self.started
        )
        lost = not self.connected and silence >= doc["heartbeat_timeout_s"]
        action, reason = "continue", "operator policy permits autonomous execution"
        if lost or self.paused_at is not None:
            if self.policy in {"return_now", "land_now"}:
                self._escalate(
                    self.policy,
                    "operator link-loss policy" if lost else "pause bounded by operator policy",
                )
            elif self.policy in {"continue_then_return", "hold_then_return"}:
                deadline = self.timeout_s + (
                    doc["heartbeat_timeout_s"] if lost and self.policy == "hold_then_return" else 0
                )
                silence_expired = lost and silence >= deadline
                pause_expired = (
                    self.paused_at is not None and self._now - self.paused_at >= self.timeout_s
                )
                if silence_expired or pause_expired:
                    self._escalate("return_now", "operator policy silence/pause timeout")
                elif self.policy == "hold_then_return" or self.paused_at is not None:
                    action, reason = "hold", "waiting for link or operator resume"
            elif self.paused_at is not None:
                action, reason = "hold", "operator pause; awaiting resume"
        budget = doc["budget"]
        remaining = budget["time_s"] - (self._now - self.started)
        if self._remaining_time is not None:
            remaining = min(remaining, self._remaining_time - (self._now - self._time_estimated_at))
        self.energy_estimate = self.energy_estimator.estimate(
            now_s=self._now,
            pose_ned=pose_ned,
            home_ne=doc["home"],
            return_altitude_m=self.executor.cruise_altitude_m
            or (max(0, -pose_ned[2]) if pose_ned is not None else 0),
            mission_remaining_s=remaining,
        )
        # Optional extra looks (small unjudged revisits) are flown only inside
        # this margin, so they never eat into the return reserve.
        margin = self.energy_estimate.get("action_margin_s")
        if (
            isinstance(margin, (int, float))
            and self.energy_estimate.get("return_energy_wh") is None
            and pose_ned is not None
        ):
            # Time budget only: that margin does not yet hold the flight home.
            c = self.energy_estimator.config
            home = doc["home"]
            margin -= (
                math.dist(pose_ned[:2], home[:2]) / max(c.cruise_speed_mps, 0.1)
                + max(0.0, -pose_ned[2]) / max(c.descent_speed_mps, 0.1)
                + c.landing_time_s
            )
        if hasattr(self.executor, "energy_margin_s"):
            self.executor.energy_margin_s = (
                float(margin)
                if isinstance(margin, (int, float)) and math.isfinite(margin)
                else None
            )
        if self.energy_estimate["action"] != "continue":
            self._escalate(self.energy_estimate["action"], self.energy_estimate["reason"])
        if remaining <= budget["reserve_time_s"]:
            self._escalate("return_now", "remaining time at or below reserve")
        if (
            self._remaining_energy is not None
            and self._remaining_energy <= budget["reserve_energy_wh"]
        ):
            self._escalate("return_now", "remaining energy at or below reserve")
        if self._low_confidence >= 3:
            self._escalate("return_now", "repeated low-confidence perception verdicts")
        if lost:
            step = self.executor.current_step
            if step is not None and step.id in doc["oversight_steps"]:
                self._escalate(
                    "return_now", f"step {step.id} requires operator oversight without link"
                )
            if pose_ned is not None:
                end = pose_ned[:2]
                if proposed_action is not None and proposed_action.kind == "translate":
                    dn, de, _ = body_to_ned(
                        proposed_action.forward_m,
                        proposed_action.right_m,
                        proposed_action.up_m,
                        yaw_deg,
                    )
                    end = (pose_ned[0] + dn, pose_ned[1] + de)
                for name, zone in doc["zones"].items():
                    if not zone.get("oversight_required") or not zone.get("risk_tag"):
                        continue
                    # Check the whole proposed segment, including crossing a zone.
                    a = pose_ned[:2]
                    d = (end[0] - a[0], end[1] - a[1])
                    c = zone["centre_ne"]
                    length2 = d[0] ** 2 + d[1] ** 2
                    t = (
                        max(0, min(1, ((c[0] - a[0]) * d[0] + (c[1] - a[1]) * d[1]) / length2))
                        if length2
                        else 0
                    )
                    if math.dist(c, (a[0] + t * d[0], a[1] + t * d[1])) <= zone["radius_m"]:
                        self._escalate(
                            "return_now",
                            f"high-risk zone {name} ({zone['risk_tag']}) "
                            "requires oversight without link",
                        )
        if self._terminal is not None:
            action, reason = self._terminal, self.reason
        elif action == "continue" and not self.plan_approved:
            # Every check above still ran: abort, link loss, energy and
            # reserves latch while the plan waits; only progress is held.
            action, reason = "hold", "awaiting operator plan approval"
        elif action == "continue" and self._retask_planning:
            action, reason = "hold", "改派规划中，保持当前位置"
        self.action, self.reason = action, reason
        if self.downlink is not None:
            self.downlink.set_connected(self.connected)
        self._record(
            "decision",
            action=action,
            reason=reason,
            connected=self.connected,
            silence_s=silence,
            step=self.executor.current_step.id if self.executor.current_step else None,
            energy=self.energy_estimate,
        )
        if self._terminal is not None and not self._finished:
            # The search is over once a return/land latches: report the result
            # now, while the link is likely still up, instead of after the
            # recovery flight (which a host may stop before it completes).
            self.finish("ended_early")
        return action

    def observe(self, *args, **kwargs):
        if self.evaluate(pose_ned=kwargs.get("pose_ned")) != "continue":
            return []
        return self.executor.observe(*args, **kwargs)

    def apply_verdict(self, track_id, decision, confidence=None, **kwargs):
        bad = decision is None or (confidence is not None and confidence < 0.5)
        self._low_confidence = self._low_confidence + 1 if bad else 0
        self.executor.apply_verdict(track_id, decision, confidence, **kwargs)
        self._time()
        self._record(
            "perception_verdict",
            track_id=track_id,
            decision=decision,
            confidence=confidence,
            findings=self.executor.findings,
            report=asdict(self.executor.report()),
        )

    def record_perception_error(self, track_id, error) -> None:
        """Log why a slow identity call produced no decision.

        A missing decision counts toward the repeated-low-confidence return;
        without the cause a log cannot tell a server fault from an
        unparseable model answer.
        """
        self._record("perception_error", track_id=track_id, error=str(error))

    def record_slow_model_unavailable(self, error_type: str, error: str) -> None:
        """Record a transport/runtime fault without creating an identity vote."""
        self._time()
        self._record(
            "slow_model_unavailable",
            reason=f"慢模型不可用: {error_type}: {error}",
            at_s=self._now,
        )

    def _recover(self, pose_ned) -> MissionExecutor:
        skill = "land" if self.action == "land_now" else "return_home"
        if self._recovery is None or self._recovery_kind != skill:
            plan = validate_mission_plan(
                {
                    "plan_version": 1,
                    "goal": "mission safety recovery",
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
            self._recovery = MissionExecutor(
                plan,
                loop_factory=self.executor.loop_factory,
                refs=self.package.refs,
                config=self.executor.config,
                mission_package=self.package,
                no_fly_zones=getattr(self.executor, "no_fly_zones", ()),
                obstacle_source=self.executor.obstacle_source,
            )
            self._recovery.home = tuple(self.package.document["home"])
            if self._recovery.airframe_adapter is not None:
                self._recovery.airframe_adapter.safety.set_home(self._recovery.home)
            self._recovery.cruise_altitude_m = self.executor.cruise_altitude_m or max(
                0, -pose_ned[2]
            )
            self._recovery_kind = skill
        return self._recovery

    def next_action(self, **kwargs):
        if self.executor.cruise_altitude_m is None:
            area = self.package.document["operating_area"]
            self.executor.cruise_altitude_m = min(
                area["max_altitude_m"], max(area["min_altitude_m"], -kwargs["pose_ned"][2])
            )
        self.evaluate(pose_ned=kwargs["pose_ned"])
        if self.action == "continue":
            result = self.executor.next_action(**kwargs)
            self.evaluate(
                pose_ned=kwargs["pose_ned"], yaw_deg=kwargs["yaw_deg"], proposed_action=result[0]
            )
        else:
            result = None
        if self.action == "hold" or self._brake_pending:
            self._brake_pending = False
            result = ActionPrimitive.hold(self.executor.config.hold_s), {}
        elif self.action in {"return_now", "land_now"}:
            result = self._recover(kwargs["pose_ned"]).next_action(**kwargs)
        action, info = result
        info = {
            **info,
            "supervisor_action": self.action,
            "supervisor_reason": self.reason,
            "energy": self.energy_estimate,
        }
        if self.recovery_sequence:
            info.update(
                recovery_action=self.recovery_action,
                recovery_sequence=list(self.recovery_sequence),
            )
        self._record(
            "execution",
            action=action.to_dict(),
            state=info,
            report=asdict(self.executor.report()),
            findings=self.executor.findings,
            history=self.executor.history,
            counts=self.executor.counts,
        )
        if self.checkpoint_store is not None and self.executor.current_step is not None:
            self._checkpoint_sequence += 1
            document = checkpoint_from_state(
                mission_id=self.package.mission_id, package_digest=self.package_digest,
                step_id=self.executor.current_step.id, phase=getattr(self.executor._run, "phase", "running"),
                sequence=self._checkpoint_sequence, counts=self.executor.counts,
                alerts=[{"value": alert} for alert in self.executor.alerts],
                visited=getattr(self.executor._run, "coverage_points", []), home=self.executor.home,
                elapsed_s=getattr(self.executor._run, "elapsed_s", 0.0), energy=self.energy_estimate,
                provenance={"source": "mission_supervisor", "plan_sha256": self.plan_sha256},
            )
            try:
                self.checkpoint_store.save(document)
            except (OSError, ValueError, TypeError) as exc:
                self._record("checkpoint_error", reason_zh=f"检查点写入失败：{exc}")
        for event in self.executor.drain_protocol_events():
            self._record(event.pop("kind", "finding"), **event)
        if self.done and not self._finished:
            self.finish("completed" if self._terminal is None else "recovery_finished")
        return action, info

    def finish(self, status: str = "stopped") -> None:
        """Write the mission's one ``final_summary`` record (idempotent).

        Called on completion, when a return/land latches (``ended_early``), on
        plan rejection and by the host when it stops the mission (``stopped``).
        """
        with self._finish_lock:
            if self._finished:
                return
            self._time()
            executor = self.executor
            lower_bounds = count_lower_bounds(executor)
            report = asdict(executor.report())
            report.update(history=executor.history, counts=executor.counts, findings=executor.findings,
                          alerts=list(executor.alerts), plan_history=list(self.plan_history), final_summary={
                              "status": status,
                              "terminal_action": self._terminal,
                              "terminal_reason": self.reason,
                              "plan_history": list(self.plan_history),
                          })
            try:
                operator_summary, summary_zh, summary_en = summarize_mission(
                    report, executor.plan,
                    self.package.document.get("task_text") or executor.plan.goal,
                )
            except Exception as exc:  # the final record must be written regardless
                operator_summary = {"error": f"{type(exc).__name__}: {exc}"}
                summary_zh = f"任务结束（{status}），摘要生成失败：{type(exc).__name__}"
                summary_en = f"Mission finished ({status}); summary rendering failed: {type(exc).__name__}"
            summary = {
                "status": status,
                "action": self.action,
                "reason": self.reason,
                "terminal_action": self._terminal,
                "terminal_reason": self.reason if self._terminal is not None else None,
                "report": asdict(executor.report()),
                "findings": executor.findings,
                "alerts": list(executor.alerts),
                "history": executor.history,
                "counts": executor.counts,
                "count_lower_bounds": lower_bounds,
                "count_is_lower_bound": any(lower_bounds.values()) if lower_bounds else None,
                "plan_history": list(self.plan_history),
                "recovery_history": self._recovery.history if self._recovery else [],
                "operator_summary": operator_summary,
                "summary_zh": summary_zh,
                "summary_en": summary_en,
            }
            summary.update(getattr(self, "test_hook_metadata", {}))
            self.log.finish(
                self._now,
                summary,
            )
            self._finished = True
        if self.downlink is not None:
            self.downlink.notify()

    def approve_plan(self, plan_sha256: str) -> bool:
        """Unsigned local convenience that takes the ``ground_event`` path.

        With a ground command key configured this fails closed: a remote
        approval must arrive as a signed ``approve_plan`` document.
        """
        return self.ground_event(
            {
                "mission_id": self.package.mission_id,
                "type": "approve_plan",
                "plan_sha256": plan_sha256,
            }
        )

    def reject_plan(self, plan_sha256: str) -> bool:
        """As :meth:`approve_plan`; a rejection latches ``land_now``."""
        return self.ground_event(
            {
                "mission_id": self.package.mission_id,
                "type": "reject_plan",
                "plan_sha256": plan_sha256,
            }
        )
