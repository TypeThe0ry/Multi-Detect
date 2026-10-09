"""Data-only proposed/approved/submitted/observed trajectory evidence.

This is not the frozen planned-supervision schema and never builds an Actor.
Only original prompt/current/past RGB may enter an Actor; endpoint poses,
teacher commands, future RGB and outcomes remain external training evidence.
Hash syntax and caller-declared ``observed_*`` origins are not authentication.
An outer verifier must reopen source bytes and check each JSON projection,
issuer, approval and execution identity before any separate training admission.

Quaternions are Hamilton wxyz active rotations: world = R(q) * body_FRD.
World positions are metres in a common, explicitly named NED or ENU origin.
Times share one declared observer-monotonic epoch, not interchangeable sensor
boot/wall clocks. STOP is navigation-sequence stop, not wait/hover/completion/RTL.
No command transport, safety decision, filesystem I/O, ML or flight is provided.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from math import atan2, hypot, pi, sqrt
from pathlib import PurePosixPath
from uuid import UUID

from .onboard_navigation_inference import PlannedNavigationTarget
from .serialization import (
    AutonomyContractError,
    canonical_json,
    canonical_sha256,
    require_int,
    require_real,
    require_schema_version,
    require_sha256,
    require_text,
    strict_json_loads,
    strict_object,
)

CONTRACT = "multidetect.onboard-executed-trajectory-step-v1"
QUATERNION_CONVENTION = "active_body_frd_to_world_wxyz"
TIME_BASIS = "observer_monotonic"
MAX_JSON_BYTES = 256 * 1024
_WORLD_FRAMES = frozenset({"ned", "enu"})


class ExecutedTrajectoryError(AutonomyContractError):
    """Malformed or internally inconsistent evidence, not a flight decision."""


def _text(value, name, limit=256):
    if type(value) is not str or require_text(value, name) != value:
        raise ExecutedTrajectoryError(f"{name} must be canonical nonblank text")
    if len(value.encode("utf-8")) > limit:
        raise ExecutedTrajectoryError(f"{name} exceeds its UTF-8 bound")
    return value


def _sha(value, name):
    if type(value) is not str or require_sha256(value, name) != value:
        raise ExecutedTrajectoryError(f"{name} must be lowercase SHA-256")
    return value


def _time(value):
    return require_real(value, "at_s", minimum=0)


def _command_id(value):
    _text(value, "command_id")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ExecutedTrajectoryError(
            "command_id must be a host transaction UUID, not MAV_CMD"
        ) from exc
    if str(parsed) != value or parsed.int == 0:
        raise ExecutedTrajectoryError("command_id must be a canonical nonnil UUID")


def _vector(value, size, name):
    if type(value) is not tuple or len(value) != size:
        raise ExecutedTrajectoryError(f"{name} needs an immutable {size}-tuple")
    return tuple(require_real(v, name, minimum=-1e9, maximum=1e9) for v in value)


def _quaternion(value):
    q = _vector(value, 4, "quaternion_wxyz")
    if abs(sum(v * v for v in q) - 1.0) > 1e-6:
        raise ExecutedTrajectoryError("quaternion must already be unit length; no normalization")
    return q


def _pointer(value):
    if type(value) is not str or len(value.encode("utf-8")) > 2048:
        raise ExecutedTrajectoryError("JSON pointer must be bounded RFC6901 text")
    if value and not value.startswith("/"):
        raise ExecutedTrajectoryError("JSON pointer must be root or begin with slash")
    for token in value.split("/"):
        i = 0
        while i < len(token):
            if token[i] == "~":
                if i + 1 == len(token) or token[i + 1] not in "01":
                    raise ExecutedTrajectoryError("invalid RFC6901 escape")
                i += 1
            i += 1


def _typed(value, cls, name):
    if type(value) is not cls:
        raise TypeError(f"{name} must be {cls.__name__}")


def _json_source(source):
    _typed(source, EvidenceBinding, "source")
    _pointer(source.json_pointer)


class _Record:
    __slots__ = ()

    def to_mapping(self):
        def convert(value):
            if isinstance(value, (_Record, PlannedNavigationTarget)):
                return value.to_mapping()
            return list(value) if type(value) is tuple else value

        return {item.name: convert(getattr(self, item.name)) for item in fields(self)}

    @property
    def canonical_hash(self):
        return canonical_sha256(self.to_mapping())

    @classmethod
    def _read(cls, value, *, nested=None, vectors=()):
        raw = dict(
            strict_object(value, name=cls.__name__, required=frozenset(f.name for f in fields(cls)))
        )
        for name, typed in (nested or {}).items():
            if raw[name] is not None:
                raw[name] = typed.from_mapping(raw[name])
        for name in vectors:
            if type(raw[name]) is not list:
                raise ExecutedTrajectoryError(f"{name} must be a JSON array")
            raw[name] = tuple(raw[name])
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class EvidenceBinding(_Record):
    """Declared immutable source identity; this class does not read the file."""

    path: str
    bytes: int
    sha256: str
    source_id: str
    json_pointer: str | None

    def __post_init__(self):
        _text(self.path, "path", 1024)
        path = PurePosixPath(self.path)
        if (
            path.is_absolute()
            or "\\" in self.path
            or ":" in self.path
            or any(p in {"", ".", ".."} for p in self.path.split("/"))
        ):
            raise ExecutedTrajectoryError("source path must be a canonical relative POSIX path")
        require_int(self.bytes, "source bytes", minimum=1)
        _sha(self.sha256, "source sha256")
        _text(self.source_id, "source_id")
        if self.json_pointer is not None:
            _pointer(self.json_pointer)

    @classmethod
    def from_mapping(cls, value):
        return cls._read(value)


@dataclass(frozen=True, slots=True)
class ActionStage(_Record):
    phase: str
    at_s: float
    clock_id: str
    action_sha256: str
    source: EvidenceBinding
    command_id: str | None

    def __post_init__(self):
        if type(self.phase) is not str or self.phase not in {"proposed", "approved", "submitted"}:
            raise ExecutedTrajectoryError("unsupported action stage")
        object.__setattr__(self, "at_s", _time(self.at_s))
        _text(self.clock_id, "clock_id")
        _sha(self.action_sha256, "action_sha256")
        _json_source(self.source)
        if (self.phase == "submitted") != (self.command_id is not None):
            raise ExecutedTrajectoryError("only submitted stage requires an execution command ID")
        if self.command_id is not None:
            _command_id(self.command_id)

    @classmethod
    def from_mapping(cls, value):
        return cls._read(value, nested={"source": EvidenceBinding})


@dataclass(frozen=True, slots=True)
class ObservedPose(_Record):
    position_m: tuple[float, float, float]
    quaternion_wxyz: tuple[float, float, float, float]
    world_frame: str
    world_origin_id: str
    quaternion_convention: str
    origin: str
    at_s: float
    clock_id: str
    source: EvidenceBinding

    def __post_init__(self):
        object.__setattr__(self, "position_m", _vector(self.position_m, 3, "position_m"))
        object.__setattr__(self, "quaternion_wxyz", _quaternion(self.quaternion_wxyz))
        if type(self.world_frame) is not str or self.world_frame not in _WORLD_FRAMES:
            raise ExecutedTrajectoryError("pose world frame must explicitly be ned or enu")
        _text(self.world_origin_id, "world_origin_id")
        if self.quaternion_convention != QUATERNION_CONVENTION:
            raise ExecutedTrajectoryError("unsupported quaternion order/direction/body axes")
        if type(self.origin) is not str or self.origin not in {
            "measured_observation",
            "simulated_observation",
        }:
            raise ExecutedTrajectoryError("planned pose is not actual observation evidence")
        object.__setattr__(self, "at_s", _time(self.at_s))
        _text(self.clock_id, "clock_id")
        _json_source(self.source)

    @classmethod
    def from_mapping(cls, value):
        return cls._read(
            value, nested={"source": EvidenceBinding}, vectors=("position_m", "quaternion_wxyz")
        )


@dataclass(frozen=True, slots=True)
class ActualObservation(_Record):
    """Raw image plus source-bound camera metadata and observed pose.

    ``source`` points to a separate frame-record JSON object containing this
    record's fields except ``source`` itself. That object embeds image and pose
    bindings, not its own file SHA: pose/events -> frames -> outcome is acyclic.
    A byte-bound image alone does not authenticate sequence or capture time.
    """

    frame_id: str
    sequence: int
    at_s: float
    clock_id: str
    image_source: EvidenceBinding
    pose: ObservedPose
    source: EvidenceBinding

    def __post_init__(self):
        _text(self.frame_id, "frame_id")
        require_int(self.sequence, "sequence", minimum=0)
        object.__setattr__(self, "at_s", _time(self.at_s))
        _text(self.clock_id, "clock_id")
        _typed(self.image_source, EvidenceBinding, "image_source")
        if self.image_source.json_pointer is not None:
            raise ExecutedTrajectoryError("RGB binding must identify raw file bytes, not JSON")
        _typed(self.pose, ObservedPose, "pose")
        _json_source(self.source)
        if self.pose.clock_id != self.clock_id:
            raise ExecutedTrajectoryError("observation and pose clocks differ")

    @classmethod
    def from_mapping(cls, value):
        return cls._read(
            value,
            nested={
                "image_source": EvidenceBinding,
                "pose": ObservedPose,
                "source": EvidenceBinding,
            },
        )


@dataclass(frozen=True, slots=True)
class ExecutionOutcome(_Record):
    status: str
    at_s: float
    clock_id: str
    action_sha256: str
    command_id: str
    source: EvidenceBinding
    before_observation_sha256: str | None
    after_observation_sha256: str | None

    def __post_init__(self):
        if type(self.status) is not str or self.status not in {
            "observed_success",
            "failed",
            "timed_out",
            "unknown",
        }:
            raise ExecutedTrajectoryError("outcome is not mission completion or command acceptance")
        object.__setattr__(self, "at_s", _time(self.at_s))
        _text(self.clock_id, "clock_id")
        _command_id(self.command_id)
        _sha(self.action_sha256, "action_sha256")
        _json_source(self.source)
        for name in ("before_observation_sha256", "after_observation_sha256"):
            value = getattr(self, name)
            if value is not None:
                _sha(value, name)
        if self.status == "observed_success" and (
            self.before_observation_sha256 is None or self.after_observation_sha256 is None
        ):
            raise ExecutedTrajectoryError("observed success requires both endpoint bindings")

    @classmethod
    def from_mapping(cls, value):
        return cls._read(value, nested={"source": EvidenceBinding})


def convert_world_vector(vector, source_frame, target_frame):
    """Metre-vector NED↔ENU conversion; no local-origin or body-FLU guessing."""
    v = _vector(vector, 3, "world vector")
    if source_frame not in _WORLD_FRAMES or target_frame not in _WORLD_FRAMES:
        raise ExecutedTrajectoryError("unsupported world-frame conversion")
    return v if source_frame == target_frame else (v[1], v[0], -v[2])


def _multiply(a, b):
    w, x, y, z = a
    v, i, j, k = b
    return (
        w * v - x * i - y * j - z * k,
        w * i + x * v + y * k - z * j,
        w * j - x * k + y * v + z * i,
        w * k + x * j - y * i + z * v,
    )


def convert_world_quaternion(quaternion, source_frame, target_frame):
    """Change world axes only, by left multiplication; body remains FRD."""
    q = _quaternion(quaternion)
    convert_world_vector((0.0, 0.0, 0.0), source_frame, target_frame)
    return q if source_frame == target_frame else _multiply((0.0, sqrt(0.5), sqrt(0.5), 0.0), q)


def gazebo_flu_enu_to_frd_ned(position_enu_m, quaternion_xyzw):
    """Explicit raw Gazebo FLU→ENU xyzw to FRD→NED wxyz conversion.

    This mathematical projection does not authenticate a source or clock and
    must be recorded as derived, never equated to original raw pose fields.
    Body-axis change is RIGHT multiplication; world-axis change is LEFT.
    """
    x, y, z, w = _quaternion(quaternion_xyzw)
    q_enu_frd = _multiply((w, x, y, z), (0.0, 1.0, 0.0, 0.0))
    return (
        convert_world_vector(position_enu_m, "enu", "ned"),
        convert_world_quaternion(q_enu_frd, "enu", "ned"),
    )


def _matrix(q):
    w, x, y, z = q
    return (
        (1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)),
        (2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)),
        (2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)),
    )


@dataclass(frozen=True, slots=True)
class ExecutedTrajectoryStep(_Record):
    step_id: str
    episode_id: str
    vehicle_id: str
    clock_id: str
    context_sha256: str
    actor_input_sha256: str
    teacher_target: PlannedNavigationTarget
    proposed: ActionStage
    approved: ActionStage | None
    submitted: ActionStage | None
    outcome: ExecutionOutcome | None
    before: ActualObservation | None
    after: ActualObservation | None
    max_pose_skew_s: float

    def __post_init__(self):
        for name in ("step_id", "episode_id", "vehicle_id", "clock_id"):
            _text(getattr(self, name), name)
        for name in ("context_sha256", "actor_input_sha256"):
            _sha(getattr(self, name), name)
        _typed(self.teacher_target, PlannedNavigationTarget, "teacher_target")
        _typed(self.proposed, ActionStage, "proposed")
        skew = require_real(self.max_pose_skew_s, "max_pose_skew_s", minimum=0, maximum=1)
        object.__setattr__(self, "max_pose_skew_s", skew)
        previous = None
        missing = False
        for phase in ("proposed", "approved", "submitted"):
            event = getattr(self, phase)
            if event is None:
                missing = True
                continue
            _typed(event, ActionStage, phase)
            if missing or event.phase != phase:
                raise ExecutedTrajectoryError("action stages must form a strict ordered prefix")
            if event.clock_id != self.clock_id or event.action_sha256 != self.action_sha256:
                raise ExecutedTrajectoryError("stage clock or teacher action identity differs")
            if previous is not None and event.at_s < previous:
                raise ExecutedTrajectoryError("action stage chronology regressed")
            previous = event.at_s
        for name in ("before", "after"):
            observation = getattr(self, name)
            if observation is not None:
                _typed(observation, ActualObservation, name)
                if observation.clock_id != self.clock_id:
                    raise ExecutedTrajectoryError("endpoint clock differs")
                if abs(observation.pose.at_s - observation.at_s) > skew:
                    raise ExecutedTrajectoryError(
                        "pose and RGB temporal anchors exceed declared skew"
                    )
        if (
            self.before is not None
            and max(self.before.at_s, self.before.pose.at_s) > self.proposed.at_s
        ):
            raise ExecutedTrajectoryError("future observation/pose cannot anchor teacher input")
        if self.after is not None:
            if self.before is None or self.submitted is None or self.outcome is None:
                raise ExecutedTrajectoryError(
                    "after observation needs before, submission and outcome"
                )
            if (
                self.after.at_s <= self.before.at_s
                or min(self.after.at_s, self.after.pose.at_s) < self.submitted.at_s
                or self.after.sequence <= self.before.sequence
                or self.after.frame_id == self.before.frame_id
            ):
                raise ExecutedTrajectoryError("actual endpoints are not ordered around submission")
            if (
                self.before.pose.world_origin_id != self.after.pose.world_origin_id
                or self.before.pose.origin != self.after.pose.origin
                or self.before.pose.source.source_id != self.after.pose.source.source_id
                or self.before.image_source.source_id != self.after.image_source.source_id
            ):
                raise ExecutedTrajectoryError("endpoint sensor/origin domain changed")
        if self.outcome is not None:
            _typed(self.outcome, ExecutionOutcome, "outcome")
            if self.submitted is None:
                raise ExecutedTrajectoryError("submission is not optional for an execution outcome")
            if (
                self.outcome.clock_id != self.clock_id
                or self.outcome.action_sha256 != self.action_sha256
                or self.outcome.command_id != self.submitted.command_id
                or self.outcome.at_s < self.submitted.at_s
            ):
                raise ExecutedTrajectoryError(
                    "execution receipt command/action/clock anchor differs"
                )
            for name in ("before", "after"):
                observation = getattr(self, name)
                expected = None if observation is None else observation.canonical_hash
                if getattr(self.outcome, f"{name}_observation_sha256") != expected:
                    raise ExecutedTrajectoryError("execution receipt endpoint identity differs")
            if (
                self.after is not None
                and max(self.after.at_s, self.after.pose.at_s) > self.outcome.at_s
            ):
                raise ExecutedTrajectoryError("outcome cannot precede actual endpoint observation")
            if self.outcome.status == "observed_success" and (
                self.before is None or self.after is None
            ):
                raise ExecutedTrajectoryError("observed success requires actual endpoints")

    @property
    def action_sha256(self):
        return canonical_sha256(self.teacher_target.to_mapping())

    def to_mapping(self):
        return {
            "schema_version": 1,
            "contract": CONTRACT,
            "time_basis": TIME_BASIS,
            **_Record.to_mapping(self),
        }

    @classmethod
    def from_mapping(cls, value):
        raw = dict(
            strict_object(
                value,
                name=cls.__name__,
                required=frozenset(f.name for f in fields(cls))
                | {"schema_version", "contract", "time_basis"},
            )
        )
        require_schema_version(raw.pop("schema_version"))
        if raw.pop("contract") != CONTRACT or raw.pop("time_basis") != TIME_BASIS:
            raise ExecutedTrajectoryError("unsupported trajectory/time contract")
        return cls._read(
            raw,
            nested={
                "teacher_target": PlannedNavigationTarget,
                "proposed": ActionStage,
                "approved": ActionStage,
                "submitted": ActionStage,
                "outcome": ExecutionOutcome,
                "before": ActualObservation,
                "after": ActualObservation,
            },
        )

    @classmethod
    def from_json(cls, payload):
        if type(payload) not in (str, bytes):
            raise TypeError("trajectory JSON must be immutable UTF-8 text or bytes")
        size = len(payload.encode("utf-8")) if type(payload) is str else len(payload)
        if not 0 < size <= MAX_JSON_BYTES:
            raise ExecutedTrajectoryError("trajectory JSON exceeds byte budget")
        return cls.from_mapping(strict_json_loads(payload, name="executed trajectory"))

    def to_json(self):
        value = canonical_json(self.to_mapping())
        if len(value.encode("utf-8")) > MAX_JSON_BYTES:
            raise ExecutedTrajectoryError("trajectory JSON exceeds byte budget")
        return value

    def audit_status(self):
        """Structural eligibility only; no implicit issuer/source/flight authority."""
        success = self.outcome is not None and self.outcome.status == "observed_success"
        return {
            "structurally_admissible": True,
            "latest_stage": self.outcome.status
            if self.outcome
            else ("submitted" if self.submitted else "approved" if self.approved else "proposed"),
            "observed_success_recorded": success,
            "training_evidence_structurally_eligible": success
            and (self.teacher_target.metric_known or self.teacher_target.kind == "stop"),
            "provenance_verified": False,
            "train_admitted": False,
            "execution_authority": False,
            "mission_complete_verified": False,
        }

    def observed_motion(self):
        """Endpoint-derived feedback, never a repaired/equated teacher command.

        Displacement uses the full BEFORE attitude. Relative quaternion retains
        roll/pitch; heading difference is null when either forward axis is vertical.
        Endpoints do not prove the continuous path, clearance or causal attribution.
        """
        if self.before is None or self.after is None:
            raise ExecutedTrajectoryError("observed motion requires two actual endpoints")
        a, b = self.before.pose, self.after.pose
        pa = convert_world_vector(a.position_m, a.world_frame, "ned")
        pb = convert_world_vector(b.position_m, b.world_frame, "ned")
        qa = convert_world_quaternion(a.quaternion_wxyz, a.world_frame, "ned")
        qb = convert_world_quaternion(b.quaternion_wxyz, b.world_frame, "ned")
        ra, rb = _matrix(qa), _matrix(qb)
        delta = tuple(y - x for x, y in zip(pa, pb, strict=True))
        frd = tuple(sum(ra[j][i] * delta[j] for j in range(3)) for i in range(3))
        yaw = None
        if hypot(ra[0][0], ra[1][0]) > 1e-8 and hypot(rb[0][0], rb[1][0]) > 1e-8:
            angle = atan2(rb[1][0], rb[0][0]) - atan2(ra[1][0], ra[0][0])
            yaw = (angle + pi) % (2 * pi) - pi
        relative = _multiply((qa[0], -qa[1], -qa[2], -qa[3]), qb)
        return {
            "translation_frame": "body_frd_at_before_observation",
            "translation_m": list(frd),
            "yaw_convention": "clockwise_right_positive",
            "yaw_delta_rad": yaw,
            "relative_quaternion_wxyz": list(relative),
            "dt_s": self.after.at_s - self.before.at_s,
            "before_observation_sha256": self.before.canonical_hash,
            "after_observation_sha256": self.after.canonical_hash,
            "origin": "actual_endpoint_difference_not_teacher_command",
            "continuous_path_verified": False,
            "causal_execution_verified": False,
        }


__all__ = [
    "CONTRACT",
    "QUATERNION_CONVENTION",
    "TIME_BASIS",
    "MAX_JSON_BYTES",
    "ExecutedTrajectoryError",
    "EvidenceBinding",
    "ActionStage",
    "ObservedPose",
    "ActualObservation",
    "ExecutionOutcome",
    "ExecutedTrajectoryStep",
    "convert_world_vector",
    "convert_world_quaternion",
    "gazebo_flu_enu_to_frd_ned",
]
