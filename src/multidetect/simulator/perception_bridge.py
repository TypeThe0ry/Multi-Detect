"""Truth-free production perception feed for the interactive simulator.

The simulator has two deliberately separate edges:

* :mod:`multidetect.simulator.live_ai_bridge` consumes PX4 MAVLink state;
* this module consumes the *same production perception objects* that the
  REAL/HIL mission path already produces (``VehicleTelemetry``, unified track
  snapshots, range solutions and monocular avoidance assessments).

``SimulatorPerceptionBridge`` joins those observations into the small,
observation-only :class:`~multidetect.simulator.ai_executor.SimulatorTelemetrySnapshot`
 contract.  It is intentionally a pure adapter at the data boundary.  It does
 not import Gazebo, inspect a simulator entity, read a truth pose, or create a
 socket.  When an executor/telemetry bridge is attached, ``feed`` invokes the
 existing executor method, which in turn uses the already configured shared
 :class:`~multidetect.simulator.mavlink_control.SimulatorMavlinkControlAdapter`.
 No second transport is introduced.

The public API accepts both the project's frozen dataclasses and lightweight
mapping/object doubles.  That makes replay, Docker probes and the live runner
use the exact same conversion code::

    feed = SimulatorPerceptionBridge(executor=executor)
    feed.enable_ai()
    decision = feed.feed(vehicle_telemetry, tracks, range_solution, avoidance)

    # For a UI/evidence consumer that only needs the observation:
    snapshot = feed.build_snapshot(vehicle_telemetry, tracks, range_solution, avoidance)

Missing GPS is represented by ``NaN``/``None`` in the snapshot, as it is in
``VehicleTelemetry``.  An explicitly supplied infinity, malformed coordinate,
distance or confidence is rejected.  NaN is therefore an intentional
"sensor unavailable" sentinel only for the optional telemetry fields that use
that convention; it is never silently turned into a physical value.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final

from ..mavlink_gps import (
    MAVLINK_GPS_FIX_TYPE_MAX,
    MAVLINK_SATELLITES_UNKNOWN,
    normalize_satellites_visible,
)
from ..operator_link import MAX_RANGE_STATUS_SOURCE_AGE_S
from .ai_executor import (
    SimulatorAiDecision,
    SimulatorAiExecutor,
    SimulatorTelemetrySnapshot,
)

_EARTH_RADIUS_M: Final[float] = 6_371_000.0
_LOST_STATES = frozenset({"lost", "invalid", "none"})
_RANGE_ACCEPTED_STATES = frozenset({"valid", "degraded"})
_AVOID_STATES = frozenset({"avoid", "invalid"})
_RANGE_DATA_AGE_FIELDS: Final[tuple[str, ...]] = (
    "data_freshness_s",
    "freshness_s",
    "age_s",
)
_RANGE_SOURCE_TIME_FIELDS: Final[tuple[str, ...]] = (
    "source_captured_at_s",
    "captured_at_s",
    "observed_at_s",
    "timestamp_s",
)
_RANGE_EVALUATED_TIME_FIELDS: Final[tuple[str, ...]] = (
    "evaluated_at_s",
    "produced_at_s",
)
_RETURN_MODES = frozenset(
    {
        "rtl",
        "return",
        "return_to_launch",
        "return_requested",
        "egress",
        "auto_rtl",
    }
)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _finite(value: object) -> bool:
    return _is_number(value) and math.isfinite(float(value))


def _nan_or_none(value: object) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def _read(value: object, *names: str, default: object = None) -> object:
    """Read the first present field from a mapping or object.

    ``hasattr`` is intentionally avoided for mappings so a mapping value of
    ``None`` is distinguishable from an absent field.  Property getters that
    throw are surfaced as a malformed input rather than being silently
    replaced with simulator truth.
    """

    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return default
    for name in names:
        try:
            return getattr(value, name)
        except AttributeError:
            continue
    return default


def _present(value: object, *names: str) -> tuple[bool, object]:
    marker = object()
    result = _read(value, *names, default=marker)
    return result is not marker, result


def _number(
    value: object,
    name: str,
    *,
    default: float | None = None,
    allow_nan: bool = False,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    """Validate one numeric field and return a float or an absent default."""

    if value is None:
        return default
    if not _is_number(value):
        raise ValueError(f"{name} must be numeric or None")
    converted = float(value)
    if math.isnan(converted):
        if allow_nan:
            return default
        raise ValueError(f"{name} must be finite")
    if not math.isfinite(converted):
        raise ValueError(f"{name} must be finite")
    if minimum is not None and converted < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    if maximum is not None and converted > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return converted


def _optional_number(value: object, name: str, *, allow_nan: bool = False) -> float | None:
    return _number(value, name, allow_nan=allow_nan)


def _boolean(value: object, name: str, *, default: bool | None = None) -> bool | None:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be bool or None")
    return value


def _integer(
    value: object,
    name: str,
    *,
    default: int | None = None,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int | None:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer or None")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return int(value)


def _enum_text(value: object, name: str, *, default: str | None = None) -> str | None:
    if value is None:
        return default
    if isinstance(value, str):
        text = value.strip().lower()
    else:
        enum_value = getattr(value, "value", None)
        if not isinstance(enum_value, str):
            raise ValueError(f"{name} must be a string/enum or None")
        text = enum_value.strip().lower()
    if not text:
        raise ValueError(f"{name} cannot be empty")
    return text


def _normalize_relative_bearing(value: float | None) -> float | None:
    if value is None:
        return None
    return (float(value) + 180.0) % 360.0 - 180.0


def _normalize_absolute_bearing(value: float | None) -> float | None:
    if value is None:
        return None
    return float(value) % 360.0


def _state_text(value: object) -> str | None:
    return _enum_text(value, "state")


def _track_items(raw: object) -> tuple[object, ...]:
    """Flatten a track, track update, mapping or sequence into candidates."""

    if raw is None:
        return ()
    if isinstance(raw, Mapping):
        tracks = raw.get("tracks")
        if tracks is not None:
            return _track_items(tracks)
        return (raw,)
    tracks = _read(raw, "tracks", default=None)
    if tracks is not None and tracks is not raw:
        return _track_items(tracks)
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        return tuple(raw)
    if isinstance(raw, Iterable) and not isinstance(raw, (str, bytes, bytearray)):
        return tuple(raw)
    return (raw,)


def _range_items(raw: object) -> tuple[object, ...]:
    if raw is None:
        return ()
    if isinstance(raw, Mapping):
        for key in ("solutions", "ranges", "items"):
            if key in raw:
                return _range_items(raw[key])
        if "solution" in raw and len(raw) <= 2:
            return _range_items(raw["solution"])
        return (raw,)
    nested = _read(raw, "solutions", "ranges", default=None)
    if nested is not None and nested is not raw:
        return _range_items(nested)
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        return tuple(raw)
    return (raw,)


def _track_id(track: object) -> str | None:
    value = _read(track, "track_id", "target_id", "id", default=None)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("track_id must be a non-empty string")
    return value.strip()


def _source_mapping(value: object) -> dict[str, object]:
    """Return stable JSON-safe source metadata without serializing objects."""

    if value is None:
        return {"present": False}
    result: dict[str, object] = {"present": True, "type": type(value).__name__}
    for name in (
        "track_id",
        "target_id",
        "label",
        "state",
        "validity",
        "frame_id",
        "data_freshness_s",
        "freshness_s",
        "age_s",
        "source_captured_at_s",
        "captured_at_s",
        "observed_at_s",
        "timestamp_s",
    ):
        present, field = _present(value, name)
        if not present:
            continue
        if name in {"state", "validity"}:
            field = _enum_text(field, name, default=None)
        if isinstance(field, (str, int, float, bool)) or field is None:
            if isinstance(field, float) and not math.isfinite(field):
                field = None
            result[name] = field
    return result


@dataclass(frozen=True, slots=True)
class SimulatorPerceptionFeedResult:
    """One conversion result and optional executor decision.

    ``snapshot`` is always observation-only.  ``decision`` is ``None`` when
    the feed is detached from an executor or when the caller requested a
    snapshot-only operation.  The result deliberately carries no simulator
    truth fields or actuator transport handle.
    """

    snapshot: SimulatorTelemetrySnapshot
    decision: SimulatorAiDecision | None
    selected_target_id: str | None
    range_validity: str | None
    avoidance_state: str | None
    reasons: tuple[str, ...] = ()
    source_evidence: Mapping[str, object] | None = None

    @property
    def target_id(self) -> str | None:
        return self.selected_target_id

    def to_mapping(self) -> dict[str, object]:
        return {
            "snapshot": self.snapshot.to_mapping(),
            "decision": None if self.decision is None else self.decision.to_mapping(),
            "selected_target_id": self.selected_target_id,
            "range_validity": self.range_validity,
            "avoidance_state": self.avoidance_state,
            "reasons": list(self.reasons),
            "source_evidence": dict(self.source_evidence or {}),
            "truth_free": True,
        }


class SimulatorPerceptionBridge:
    """Convert production observation objects into the shared SIM AI contract.

    Parameters
    ----------
    executor:
        Optional :class:`SimulatorAiExecutor`.  ``feed`` calls its ``step``
        method after constructing a snapshot.  AI remains explicitly disabled
        until the caller invokes :meth:`enable_ai`.
    telemetry_bridge:
        Optional existing ``SimulatorTelemetryBridge``.  When supplied, the
        production snapshot is handed to its ``ingest_perception()`` method so
        heartbeat, armed, target-lease and watchdog gates remain identical to
        raw MAVLink telemetry.  No second transport is created; the bridge
        remains the sole owner of the executor's output.  Passing both objects
        requires them to refer to the same executor.
    clock:
        Monotonic callable used only for processing timestamps/evidence.
    prefer_ground_range:
        Select ``ground_range_m`` before ``slant_range_m`` when both are
        available.  Ground range is generally the stable horizontal control
        error for multirotor, VTOL and fixed-wing policies.
    maximum_range_age_s:
        Maximum accepted range source/data age.  The default is the shared
        operator-link ``RANGE_STATUS`` limit (1.5 seconds).  VALID/DEGRADED
        ranges must carry both data freshness and an attributable source time;
        an immutable ``RangeSolution`` uses ``evaluated_at_s`` plus its data
        age, while mapping/status inputs carry an explicit capture timestamp.
    """

    _SCHEMA_VERSION: Final[int] = 1

    def __init__(
        self,
        executor: SimulatorAiExecutor | None = None,
        *,
        telemetry_bridge: object | None = None,
        clock: Any = time.monotonic,
        prefer_ground_range: bool = True,
        maximum_range_age_s: float = MAX_RANGE_STATUS_SOURCE_AGE_S,
        maximum_history: int = 256,
    ) -> None:
        if executor is not None and not isinstance(executor, SimulatorAiExecutor):
            raise TypeError("executor must be a SimulatorAiExecutor or None")
        if telemetry_bridge is not None:
            candidate = getattr(telemetry_bridge, "executor", None)
            if not isinstance(candidate, SimulatorAiExecutor):
                raise TypeError("telemetry_bridge must expose a SimulatorAiExecutor")
            if not callable(getattr(telemetry_bridge, "ingest_perception", None)):
                raise TypeError(
                    "telemetry_bridge must expose callable ingest_perception()"
                )
            if executor is not None and candidate is not executor:
                raise ValueError("executor and telemetry_bridge must share one executor")
            executor = candidate
        if not callable(clock):
            raise TypeError("clock must be callable")
        if not isinstance(prefer_ground_range, bool):
            raise TypeError("prefer_ground_range must be bool")
        if not _finite(maximum_range_age_s) or float(maximum_range_age_s) <= 0.0:
            raise ValueError("maximum_range_age_s must be finite and positive")
        if isinstance(maximum_history, bool) or not 1 <= int(maximum_history) <= 10_000:
            raise ValueError("maximum_history must be in [1, 10000]")
        self.executor = executor
        self.telemetry_bridge = telemetry_bridge
        self.clock = clock
        self.prefer_ground_range = prefer_ground_range
        self.maximum_range_age_s = float(maximum_range_age_s)
        self._lock = threading.RLock()
        self._history: deque[dict[str, object]] = deque(maxlen=int(maximum_history))
        self._last_result: SimulatorPerceptionFeedResult | None = None
        self._observations = 0
        self._rejected = 0
        self._last_error: str | None = None
        self._last_selection_metadata: tuple[str | None, str | None, str | None] = (
            None,
            None,
            None,
        )
        self._last_reasons: tuple[str, ...] = ()
        self._last_sources: dict[str, object] = {}

    @property
    def last_result(self) -> SimulatorPerceptionFeedResult | None:
        with self._lock:
            return self._last_result

    @property
    def last_snapshot(self) -> SimulatorTelemetrySnapshot | None:
        result = self.last_result
        return None if result is None else result.snapshot

    def enable_ai(self) -> None:
        """Forward explicit AI enablement to the shared executor/bridge."""

        if self.executor is None:
            raise RuntimeError("no SimulatorAiExecutor is attached")
        self.executor.enable_ai()

    enable = enable_ai

    def release_manual_control(self) -> None:
        if self.executor is None:
            raise RuntimeError("no SimulatorAiExecutor is attached")
        self.executor.release_manual_control()

    def build_snapshot(
        self,
        telemetry: object,
        track: object | None = None,
        ranging: object | None = None,
        avoidance: object | None = None,
        *,
        tracks: object | None = None,
        selected_track_id: str | None = None,
        frame_id: str | None = None,
        frame_captured_at_s: float | None = None,
        now_s: float | None = None,
        home: object | None = None,
        home_bearing_deg: float | None = None,
        home_distance_m: float | None = None,
        return_requested: bool | None = None,
        obstacle_ahead: bool | None = None,
    ) -> SimulatorTelemetrySnapshot:
        """Build one truth-free snapshot from production observations.

        ``track`` may be one ``UnifiedTrackSnapshot``, a ``TargetPoolUpdate``,
        a mapping or a sequence.  ``ranging`` may likewise be one
        ``RangeSolution`` or a sequence; a solution is used only when its
        target matches the selected track.  ``avoidance`` accepts
        ``MonocularAvoidanceAssessment`` or a mapping containing ``state``/
        ``obstacle_ahead``.
        """

        if telemetry is None:
            raise ValueError("telemetry is required")
        if track is not None and tracks is not None:
            raise ValueError("provide track or tracks, not both")
        # Selection metadata is per observation.  Never leak a previous
        # target/range/avoidance status into a GPS-denied or target-free frame.
        self._last_selection_metadata = (None, None, None)
        self._last_reasons = ()
        track_input = tracks if tracks is not None else track
        source_time = self._telemetry_time(telemetry, now_s=now_s)
        self._last_sources = {
            "telemetry": _source_mapping(telemetry),
            "track": _source_mapping(track_input),
            "ranging": _source_mapping(ranging),
            "avoidance": _source_mapping(avoidance),
        }
        vehicle = self._vehicle_fields(telemetry, source_time)
        selected, track_info = self._select_track(
            track_input,
            selected_track_id=selected_track_id,
        )
        expected_frame_id = frame_id
        if expected_frame_id is None:
            expected_frame_id = _read(track_input, "frame_id", "source_frame_id", default=None)
        if expected_frame_id is None and selected is not None:
            expected_frame_id = _read(selected, "frame_id", "source_frame_id", default=None)
        if expected_frame_id is not None and (
            not isinstance(expected_frame_id, str) or not expected_frame_id.strip()
        ):
            raise ValueError("frame_id must be a non-empty string when supplied")
        expected_frame_time = frame_captured_at_s
        if expected_frame_time is None:
            expected_frame_time = _read(track_input, "captured_at_s", default=None)
        if expected_frame_time is None and selected is not None:
            expected_frame_time = _read(selected, "captured_at_s", default=None)
        if expected_frame_time is not None:
            expected_frame_time = self._timestamp(
                expected_frame_time,
                "frame_captured_at_s",
            )
        range_info = self._select_range(
            ranging,
            selected_track_id=track_info["track_id"],
            expected_frame_id=expected_frame_id,
            expected_frame_captured_at_s=expected_frame_time,
            now_s=source_time,
        )

        target_visible = selected is not None and track_info["state"] not in _LOST_STATES
        target_confidence = track_info["confidence"]
        if target_confidence is None:
            target_confidence = range_info["confidence"]
        elif range_info["confidence"] is not None:
            target_confidence = min(target_confidence, range_info["confidence"])
        if target_confidence is not None and not target_visible:
            target_confidence = None

        target_bearing = range_info["relative_bearing_deg"]
        if target_bearing is None and range_info["absolute_bearing_deg"] is not None:
            target_bearing = _normalize_relative_bearing(
                range_info["absolute_bearing_deg"] - vehicle["heading_deg"]
            )
        target_distance = range_info["distance_m"] if target_visible else None
        if target_distance is None and target_visible and range_info["validity"] is None:
            # A visual track without a metric range is still visible, but the
            # AI policy will remain in its bounded hover/observation gate.
            target_bearing = None

        home_values = self._home_vector(
            telemetry,
            home,
            vehicle,
            explicit_bearing=home_bearing_deg,
            explicit_distance=home_distance_m,
        )
        return_flag = self._return_flag(
            telemetry,
            explicit=return_requested,
            home=home,
        )
        obstacle_flag, avoidance_state = self._obstacle_flag(
            avoidance,
            explicit=obstacle_ahead,
        )
        self._last_selection_metadata = (
            self._last_selection_metadata[0],
            self._last_selection_metadata[1],
            avoidance_state,
        )
        try:
            return SimulatorTelemetrySnapshot(
                observed_at_s=source_time,
                altitude_agl_m=vehicle["altitude_agl_m"],
                heading_deg=vehicle["heading_deg"],
                ground_speed_mps=vehicle["ground_speed_mps"],
                latitude_deg=vehicle["latitude_deg"],
                longitude_deg=vehicle["longitude_deg"],
                gps_fix_type=vehicle["gps_fix_type"],
                gps_horizontal_accuracy_m=vehicle["gps_horizontal_accuracy_m"],
                gps_vertical_accuracy_m=vehicle["gps_vertical_accuracy_m"],
                satellites_visible=vehicle["satellites_visible"],
                target_visible=target_visible,
                target_bearing_deg=target_bearing,
                target_distance_m=target_distance,
                target_confidence=target_confidence,
                home_bearing_deg=home_values[0],
                home_distance_m=home_values[1],
                obstacle_ahead=obstacle_flag,
                link_healthy=vehicle["link_healthy"],
                armed=vehicle["armed"],
                battery_remaining_pct=vehicle["battery_remaining_pct"],
                return_requested=return_flag,
                altitude_observed_at_s=vehicle["altitude_observed_at_s"],
                position_observed_at_s=vehicle["position_observed_at_s"],
                gps_observed_at_s=vehicle["gps_observed_at_s"],
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid perception observation: {exc}") from exc

    # Friendly aliases used by integrations/replay code.
    to_snapshot = build_snapshot
    observe = build_snapshot

    def feed(
        self,
        telemetry: object,
        track: object | None = None,
        ranging: object | None = None,
        avoidance: object | None = None,
        **kwargs: object,
    ) -> SimulatorAiDecision | None:
        """Build a snapshot and optionally execute one shared AI step."""

        result = self.feed_result(telemetry, track, ranging, avoidance, **kwargs)
        return result.decision

    ingest = feed
    step = feed

    def feed_result(
        self,
        telemetry: object,
        track: object | None = None,
        ranging: object | None = None,
        avoidance: object | None = None,
        **kwargs: object,
    ) -> SimulatorPerceptionFeedResult:
        # Keep a rejection record even when the caller wants the exception to
        # propagate.  Previously malformed observations raised correctly but
        # left ``evidence()['rejected']`` at zero, which made a running SIM
        # job look healthier than the actual feed.  Recording happens under
        # the same lock used by the history snapshot and never changes the
        # exception type/message seen by the integration.
        try:
            snapshot = self.build_snapshot(telemetry, track, ranging, avoidance, **kwargs)
        except (TypeError, ValueError, RuntimeError) as exc:
            with self._lock:
                self._rejected += 1
                self._last_error = f"{type(exc).__name__}: {str(exc)[:240]}"
            raise
        with self._lock:
            selected_id, range_validity, avoidance_state = self._last_selection_metadata
        decision: SimulatorAiDecision | None = None
        bridge_rejection_reason: str | None = None
        if self.telemetry_bridge is not None:
            # A bound telemetry bridge is the canonical runtime edge.  Passing
            # the production timestamp explicitly keeps the perception sample
            # in the same replay/monotonic time domain as PX4 frames and avoids
            # replacing it with a second wall-clock cursor.
            rejected_before: int | None = None
            evidence_reader = getattr(self.telemetry_bridge, "evidence", None)
            if callable(evidence_reader):
                try:
                    before_evidence = evidence_reader()
                except (TypeError, ValueError, RuntimeError):
                    before_evidence = None
                if isinstance(before_evidence, Mapping):
                    raw_rejected = before_evidence.get("perception_rejected")
                    if isinstance(raw_rejected, int) and not isinstance(raw_rejected, bool):
                        rejected_before = raw_rejected
            try:
                decision = self.telemetry_bridge.ingest_perception(
                    snapshot,
                    timestamp_s=snapshot.observed_at_s,
                )
            except (TypeError, ValueError, RuntimeError) as exc:
                with self._lock:
                    self._rejected += 1
                    self._last_error = f"{type(exc).__name__}: {str(exc)[:240]}"
                raise
            if rejected_before is not None and callable(evidence_reader):
                try:
                    after_evidence = evidence_reader()
                except (TypeError, ValueError, RuntimeError):
                    after_evidence = None
                if isinstance(after_evidence, Mapping):
                    raw_rejected = after_evidence.get("perception_rejected")
                    rejected_after = (
                        raw_rejected
                        if isinstance(raw_rejected, int) and not isinstance(raw_rejected, bool)
                        else None
                    )
                    if rejected_after is not None and rejected_after > rejected_before:
                        # ``ingest_perception`` returns None both for a gated
                        # (not-yet-armed) observation and for a rejected one.
                        # Surface the latter as a cleared feed result so a
                        # caller inspecting this adapter cannot mistake the
                        # candidate snapshot for the bridge's current target.
                        bridge_rejection_reason = "telemetry_bridge_rejected_perception"
                        selected_id = None
                        range_validity = None
                        snapshot = replace(
                            snapshot,
                            target_visible=False,
                            target_bearing_deg=None,
                            target_distance_m=None,
                            target_confidence=None,
                        )
                        self._last_reasons = tuple(
                            (*self._last_reasons, bridge_rejection_reason)
                        )
                        with self._lock:
                            self._rejected += 1
        elif self.executor is not None:
            now_value = kwargs.get("now_s")
            step_now = None if now_value is None else self._timestamp(now_value, "now_s")
            try:
                decision = self.executor.step(snapshot, now_s=step_now)
            except (TypeError, ValueError, RuntimeError) as exc:
                with self._lock:
                    self._rejected += 1
                    self._last_error = f"{type(exc).__name__}: {str(exc)[:240]}"
                raise
        result = SimulatorPerceptionFeedResult(
            snapshot=snapshot,
            decision=decision,
            selected_target_id=selected_id,
            range_validity=range_validity,
            avoidance_state=avoidance_state,
            reasons=tuple(self._last_reasons),
            source_evidence=dict(self._last_sources),
        )
        with self._lock:
            self._observations += 1
            self._last_result = result
            self._last_error = bridge_rejection_reason
            self._history.append(result.to_mapping())
        return result

    def reset(self) -> None:
        with self._lock:
            self._history.clear()
            self._last_result = None
            self._observations = 0
            self._rejected = 0
            self._last_error = None
            self._last_sources = {}
        self._last_selection_metadata = (None, None, None)
        self._last_reasons: tuple[str, ...] = ()

    def evidence(self) -> dict[str, object]:
        with self._lock:
            result = self._last_result
            executor_evidence = None if self.executor is None else self.executor.evidence()
            return {
                "schema_version": self._SCHEMA_VERSION,
                "event": "simulator_perception_observation_feed",
                "observations": self._observations,
                "rejected": self._rejected,
                "last_error": self._last_error,
                "selected_target_id": (
                    self._last_selection_metadata[0]
                    if result is None
                    else result.selected_target_id
                ),
                "last_snapshot": None if result is None else result.snapshot.to_mapping(),
                "last_result": None if result is None else result.to_mapping(),
                "source_evidence": dict(self._last_sources),
                "recent_results": list(self._history),
                "executor_attached": self.executor is not None,
                "telemetry_bridge_attached": self.telemetry_bridge is not None,
                "output_transport": (
                    "shared SimulatorMavlinkControlAdapter MANUAL_CONTROL"
                    if self.executor is not None
                    else "observation_only"
                ),
                "truth_free": True,
                "truth_free_input_contract": True,
                "gazebo_truth_read": False,
                "secondary_transport_emitted": False,
                "flight_control_commands_created": False,
                "range_preference": "ground_then_slant"
                if self.prefer_ground_range
                else "slant_then_ground",
                "maximum_range_age_s": self.maximum_range_age_s,
                "range_freshness_contract": {
                    "data_age_fields": list(_RANGE_DATA_AGE_FIELDS),
                    "source_time_fields": list(_RANGE_SOURCE_TIME_FIELDS),
                    "evaluated_time_fields": list(_RANGE_EVALUATED_TIME_FIELDS),
                    "target_frame_binding_required": True,
                    "missing_freshness_rejected": True,
                    "fail_closed": True,
                },
                "executor": executor_evidence,
            }

    # --- conversion internals -------------------------------------------------

    def _timestamp(self, value: object, name: str) -> float:
        if not _finite(value) or float(value) < 0.0:
            raise ValueError(f"{name} must be finite and non-negative")
        return float(value)

    def _telemetry_time(self, telemetry: object, *, now_s: float | None) -> float:
        if now_s is not None:
            return self._timestamp(now_s, "now_s")
        value = _read(
            telemetry,
            "observed_at_s",
            "timestamp_s",
            "time_s",
            "captured_at_s",
            "position_observed_at_s",
            "gps_observed_at_s",
            default=None,
        )
        if value is None:
            value = self.clock()
        # VehicleTelemetry uses NaN for unavailable timestamps.  It is safe to
        # fall back to the processing clock in that one optional case.
        if isinstance(value, float) and math.isnan(value):
            value = self.clock()
        return self._timestamp(value, "telemetry timestamp")

    def _vehicle_fields(self, telemetry: object, stamp: float) -> dict[str, object]:
        def optional_float(*names: str, default: float = math.nan) -> float:
            present, value = _present(telemetry, *names)
            if not present or value is None:
                return default
            result = _number(value, names[0], default=default, allow_nan=True)
            return default if result is None else float(result)

        altitude = optional_float(
            "altitude_agl_m", "relative_altitude_m", "relative_alt_m", "altitude_m", "alt",
            default=0.0,
        )
        if altitude < 0.0:
            raise ValueError("altitude_agl_m must be non-negative")
        heading = optional_float("heading_deg", "heading", default=0.0)
        # A yaw field is commonly radians in telemetry mappings.  Only use it
        # when no explicit heading was supplied.
        if not _present(telemetry, "heading_deg", "heading")[0]:
            yaw_rad = _read(telemetry, "yaw_rad", default=None)
            if yaw_rad is not None:
                yaw = _number(yaw_rad, "yaw_rad", allow_nan=True)
                heading = 0.0 if yaw is None else math.degrees(yaw)
        heading = _normalize_absolute_bearing(heading) or 0.0

        speed = optional_float(
            "ground_speed_mps", "groundspeed", "speed_mps", "speed", "airspeed_mps", "airspeed",
            default=0.0,
        )
        if not _present(telemetry, "ground_speed_mps", "groundspeed", "speed_mps", "speed")[0]:
            airspeed = optional_float("airspeed_mps", "airspeed", default=math.nan)
            if _finite(airspeed):
                speed = max(0.0, float(airspeed))
            north = optional_float("velocity_north_mps", "velocity_north", default=math.nan)
            east = optional_float("velocity_east_mps", "velocity_east", default=math.nan)
            if speed == 0.0 and _finite(north) and _finite(east):
                speed = math.hypot(float(north), float(east))
        if speed < 0.0:
            raise ValueError("ground_speed_mps must be non-negative")

        lat = optional_float("latitude_deg", "latitude", "lat", default=math.nan)
        lon = optional_float("longitude_deg", "longitude", "lon", default=math.nan)
        lat_available = _finite(lat)
        lon_available = _finite(lon)
        if lat_available != lon_available:
            raise ValueError("latitude and longitude must be supplied together")
        # Keep telemetry input aligned with the shared target/geolocation
        # contract.  Exact poles are mathematically valid GNSS values but do
        # not provide a stable local N/E tangent frame for target projection.
        if lat_available and not -90.0 < lat < 90.0:
            raise ValueError("latitude_deg must be within (-90, 90)")
        if lon_available and not -180.0 <= lon <= 180.0:
            raise ValueError("longitude_deg must be within [-180, 180]")

        fix_raw = _read(telemetry, "gps_fix_type", "fix_type", default=None)
        fix = _integer(fix_raw, "gps_fix_type", minimum=0, maximum=MAVLINK_GPS_FIX_TYPE_MAX)
        sats_raw = _integer(
            _read(telemetry, "satellites_visible", "satellites", "sat_count", default=None),
            "satellites_visible",
            minimum=0,
            maximum=MAVLINK_SATELLITES_UNKNOWN,
        )
        # MAVLink uses 255 as an unknown satellite count, not a usable count.
        # Convert it to the project's ``None`` unavailable sentinel before the
        # observation reaches the shared AI contract.
        sats = normalize_satellites_visible(sats_raw)
        hacc = optional_float(
            "gps_horizontal_accuracy_m", "horizontal_accuracy_m", "gps_accuracy_m", default=math.nan
        )
        vacc = optional_float(
            "gps_vertical_accuracy_m", "vertical_accuracy_m", default=math.nan
        )
        if _finite(hacc) and hacc < 0.0:
            raise ValueError("gps_horizontal_accuracy_m must be non-negative")
        if _finite(vacc) and vacc < 0.0:
            raise ValueError("gps_vertical_accuracy_m must be non-negative")

        battery = optional_float(
            "battery_remaining_pct", "battery_percent", "battery_remaining", default=math.nan
        )
        if _finite(battery) and not 0.0 <= battery <= 100.0:
            raise ValueError("battery_remaining_pct must be within [0, 100]")

        armed_value = _read(telemetry, "armed", default=None)
        if armed_value is None:
            base_mode = _read(telemetry, "base_mode", default=None)
            if base_mode is not None:
                mode = _integer(base_mode, "base_mode", minimum=0, maximum=255)
                armed_value = bool(mode & 128)
        armed = _boolean(armed_value, "armed", default=None)
        link = _boolean(
            _read(telemetry, "link_healthy", "connection_healthy", "connected", default=None),
            "link_healthy",
            default=None,
        )
        position_healthy = _boolean(
            _read(telemetry, "position_healthy", "gps_healthy", default=None),
            "position_healthy",
            default=None,
        )
        if position_healthy is False:
            # Preserve the explicit health failure instead of allowing a
            # cached coordinate to qualify a return path.
            lat = lon = math.nan
            fix = sats = None
            hacc = vacc = math.nan

        altitude_time = self._optional_timestamp(
            telemetry, "altitude_observed_at_s", "relative_altitude_observed_at_s", default=stamp
        )
        position_present, _ = _present(telemetry, "position_observed_at_s")
        position_time = self._optional_timestamp(
            telemetry, "position_observed_at_s", default=stamp
        )
        if not position_present:
            # Backwards-compatible mapping support: older telemetry objects
            # exposed only the dedicated GPS timestamp.  Keep the fused
            # position timestamp populated for those producers.
            position_time = self._optional_timestamp(
                telemetry, "gps_observed_at_s", default=stamp
            )
        gps_present, _ = _present(telemetry, "gps_observed_at_s")
        gps_time = self._optional_timestamp(
            telemetry, "gps_observed_at_s", default=None
        )
        if not gps_present:
            # Legacy producers had no separate GPS cursor.  Infer one only
            # when the observation itself carries a complete fix; a generic
            # timestamp on a GPS-denied frame must not look like a fresh GPS
            # sample.
            if (
                position_healthy is not False
                and _finite(lat)
                and _finite(lon)
                and fix is not None
                and fix >= 3
            ):
                gps_time = position_time
            else:
                gps_time = None
        if position_healthy is False:
            gps_time = None
        return {
            "altitude_agl_m": altitude,
            "heading_deg": heading,
            "ground_speed_mps": speed,
            "latitude_deg": lat,
            "longitude_deg": lon,
            "gps_fix_type": fix,
            "gps_horizontal_accuracy_m": hacc,
            "gps_vertical_accuracy_m": vacc,
            "satellites_visible": sats,
            "battery_remaining_pct": battery,
            "armed": armed,
            "link_healthy": link,
            "altitude_observed_at_s": altitude_time,
            "position_observed_at_s": position_time,
            "gps_observed_at_s": gps_time,
        }

    def _optional_timestamp(
        self, value: object, *names: str, default: float | None = None
    ) -> float | None:
        present, raw = _present(value, *names)
        if not present or raw is None:
            return default
        if isinstance(raw, float) and math.isnan(raw):
            return default
        return self._timestamp(raw, names[0])

    def _select_track(
        self, raw: object, *, selected_track_id: str | None
    ) -> tuple[object | None, dict[str, object]]:
        candidates = _track_items(raw)
        explicit_primary = _read(raw, "primary_track_id", "selected_track_id", default=None)
        if explicit_primary is not None and not isinstance(explicit_primary, str):
            raise ValueError("primary_track_id must be a string or None")
        wanted = selected_track_id or explicit_primary
        if wanted is not None and (not isinstance(wanted, str) or not wanted.strip()):
            raise ValueError("selected_track_id must be a non-empty string")
        normalized: list[tuple[object, dict[str, object]]] = []
        for item in candidates:
            track_id = _track_id(item)
            if track_id is None:
                raise ValueError("track candidate requires track_id")
            confidence_raw = _read(item, "confidence", "target_confidence", default=None)
            confidence = _number(
                confidence_raw,
                "track confidence",
                allow_nan=True,
                minimum=0.0,
                maximum=1.0,
            )
            quality = _number(
                _read(item, "tracking_quality", "quality", default=None),
                "tracking_quality",
                allow_nan=True,
                minimum=0.0,
                maximum=1.0,
            )
            if confidence is not None and quality is not None:
                confidence = min(confidence, quality)
            state = _state_text(_read(item, "state", default=None))
            primary = _boolean(
                _read(item, "primary", default=False), "track.primary", default=False
            )
            actionable = _boolean(
                _read(item, "actionable", default=False), "track.actionable", default=False
            )
            locked = _boolean(_read(item, "locked", default=False), "track.locked", default=False)
            last_seen = _number(
                _read(item, "last_seen_at_s", "observed_at_s", "captured_at_s", default=0.0),
                "track timestamp",
                allow_nan=True,
                minimum=0.0,
                default=0.0,
            )
            info = {
                "track_id": track_id,
                "confidence": confidence,
                "state": state,
                "primary": bool(primary),
                "actionable": bool(actionable),
                "locked": bool(locked),
                "last_seen_at_s": 0.0 if last_seen is None else last_seen,
                "label": _read(item, "label", default=None),
            }
            normalized.append((item, info))
        chosen: tuple[object, dict[str, object]] | None = None
        if wanted is not None:
            chosen = next(
                (pair for pair in normalized if pair[1]["track_id"] == wanted.strip()),
                None,
            )
            if chosen is None:
                raise ValueError(f"selected_track_id {wanted!r} is not present")
        elif normalized:
            chosen = max(
                normalized,
                key=lambda pair: (
                    pair[1]["primary"],
                    pair[1]["actionable"],
                    pair[1]["locked"],
                    pair[1]["state"] not in _LOST_STATES,
                    -1.0 if pair[1]["confidence"] is None else pair[1]["confidence"],
                    pair[1]["last_seen_at_s"],
                    # Stable deterministic tie break, independent of input
                    # container ordering.
                    pair[1]["track_id"],
                ),
            )
        if chosen is None:
            self._last_selection_metadata = (None, None, None)
            self._last_reasons = ("target_unavailable",)
            return None, {
                "track_id": None,
                "confidence": None,
                "state": None,
                "primary": False,
                "actionable": False,
                "locked": False,
                "last_seen_at_s": 0.0,
            }
        item, info = chosen
        self._last_selection_metadata = (info["track_id"], None, None)
        self._last_reasons = ()
        return item, info

    def _select_range(
        self,
        raw: object,
        *,
        selected_track_id: object,
        expected_frame_id: object,
        expected_frame_captured_at_s: float | None,
        now_s: float,
    ) -> dict[str, object]:
        candidates = _range_items(raw)
        if not candidates:
            return {
                "validity": None,
                "distance_m": None,
                "relative_bearing_deg": None,
                "absolute_bearing_deg": None,
                "confidence": None,
                "target_id": None,
            }
        selected: object | None = None
        fallback: object | None = None
        for item in candidates:
            target_id_value = _read(item, "target_id", "track_id", "id", default=None)
            target_id = None if target_id_value is None else str(target_id_value).strip()
            if target_id and selected_track_id and target_id == selected_track_id:
                selected = item
                break
            if fallback is None:
                fallback = item
        if selected is None:
            # A target-less range mapping is allowed only when there is exactly
            # one candidate and one selected track; this keeps replay fixtures
            # convenient without allowing cross-target range leakage.
            if len(candidates) == 1:
                item = fallback
                candidate_target = _read(item, "target_id", "track_id", default=None)
                if candidate_target not in (None, "") and selected_track_id not in (None, ""):
                    return self._empty_range("range_target_mismatch")
                selected = item
            else:
                return self._empty_range("range_target_mismatch")
        validity = _enum_text(
            _read(selected, "validity", "status", default=None),
            "range validity",
            default="valid",
        )
        if validity not in _RANGE_ACCEPTED_STATES and validity not in {"invalid", "unavailable"}:
            raise ValueError(f"unsupported range validity {validity!r}")
        if validity in _RANGE_ACCEPTED_STATES:
            freshness_reason = self._range_freshness_reason(
                selected,
                expected_frame_id=expected_frame_id,
                expected_frame_captured_at_s=expected_frame_captured_at_s,
                now_s=now_s,
            )
            if freshness_reason is not None:
                return self._empty_range(freshness_reason)
        confidence = _number(
            _read(selected, "confidence", "range_confidence", "sensor_consistency", default=None),
            "range confidence",
            allow_nan=True,
            minimum=0.0,
            maximum=1.0,
        )
        relative = _number(
            _read(selected, "relative_bearing_deg", "bearing_deg", "bearing", default=None),
            "relative_bearing_deg",
            allow_nan=True,
        )
        absolute = _number(
            _read(selected, "absolute_bearing_deg", "absolute_bearing", default=None),
            "absolute_bearing_deg",
            allow_nan=True,
        )
        distance_values: list[tuple[str, float | None]] = []
        ground = _number(
            _read(selected, "ground_range_m", "ground_distance_m", default=None),
            "ground_range_m",
            allow_nan=True,
            minimum=0.0,
        )
        slant = _number(
            _read(selected, "slant_range_m", "distance_m", "range_m", default=None),
            "slant_range_m",
            allow_nan=True,
            minimum=0.0,
        )
        if self.prefer_ground_range:
            distance_values.extend((("ground", ground), ("slant", slant)))
        else:
            distance_values.extend((("slant", slant), ("ground", ground)))
        distance = next((value for _, value in distance_values if value is not None), None)
        if validity not in _RANGE_ACCEPTED_STATES:
            distance = None
            relative = None
            absolute = None
            # An invalid solution is evidence that metric ranging was not
            # accepted; its default ``sensor_consistency=0`` must not erase a
            # perfectly valid visual-track confidence.
            confidence = None
        if relative is not None:
            relative = _normalize_relative_bearing(relative)
        if absolute is not None:
            absolute = _normalize_absolute_bearing(absolute)
        self._last_selection_metadata = (
            self._last_selection_metadata[0],
            validity,
            self._last_selection_metadata[2],
        )
        return {
            "validity": validity,
            "distance_m": distance,
            "relative_bearing_deg": relative,
            "absolute_bearing_deg": absolute,
            "confidence": confidence,
            "target_id": _read(selected, "target_id", "track_id", default=None),
        }

    def _range_freshness_reason(
        self,
        selected: object,
        *,
        expected_frame_id: object,
        expected_frame_captured_at_s: float | None,
        now_s: float,
    ) -> str | None:
        """Validate the complete target/frame/time binding for metric range."""

        present, raw_frame_id = _present(selected, "frame_id", "source_frame_id")
        if not present or raw_frame_id is None:
            return "range_frame_id_missing"
        if not isinstance(raw_frame_id, str) or not raw_frame_id.strip():
            raise ValueError("range frame_id must be a non-empty string")
        range_frame_id = raw_frame_id.strip()
        if expected_frame_id is not None and range_frame_id != str(expected_frame_id).strip():
            return "range_frame_mismatch"

        data_age: float | None = None
        for field_name in _RANGE_DATA_AGE_FIELDS:
            present, raw_age = _present(selected, field_name)
            if not present:
                continue
            if raw_age is None:
                return "range_data_freshness_missing"
            data_age = _number(
                raw_age,
                f"range {field_name}",
                minimum=0.0,
            )
            break
        if data_age is None:
            return "range_data_freshness_missing"
        if data_age > self.maximum_range_age_s:
            return "range_data_stale"

        source_time: float | None = None
        explicit_source_time = False
        for field_name in _RANGE_SOURCE_TIME_FIELDS:
            present, raw_timestamp = _present(selected, field_name)
            if not present:
                continue
            if raw_timestamp is None:
                return "range_source_timestamp_missing"
            source_time = self._timestamp(raw_timestamp, f"range {field_name}")
            explicit_source_time = True
            break

        evaluated_time: float | None = None
        for field_name in _RANGE_EVALUATED_TIME_FIELDS:
            present, raw_timestamp = _present(selected, field_name)
            if not present:
                continue
            if raw_timestamp is None:
                return "range_source_timestamp_missing"
            evaluated_time = self._timestamp(raw_timestamp, f"range {field_name}")
            break

        if source_time is None and expected_frame_captured_at_s is not None:
            source_time = expected_frame_captured_at_s
        if source_time is None and evaluated_time is not None:
            # ``RangeSolution`` freezes the maximum contributing-data age at
            # evaluation.  Reconstructing its oldest possible source time is
            # conservative and, unlike comparing data_freshness_s alone,
            # continues ageing while the immutable solution is cached.
            source_time = evaluated_time - data_age
        if source_time is None:
            return "range_source_timestamp_missing"

        # A caller may not attach an already evaluated RangeSolution to a
        # later camera frame.  ``RangeSolution.frame_id`` binds identity and
        # this ordering check binds the matching capture time; effective data
        # age below then keeps ageing the frozen solution on every reuse.
        if evaluated_time is not None and source_time > evaluated_time + 1.0e-6:
            return "range_frame_timestamp_mismatch"
        if (
            expected_frame_captured_at_s is not None
            and evaluated_time is not None
            and expected_frame_captured_at_s > evaluated_time + 1.0e-6
        ):
            return "range_frame_timestamp_mismatch"

        source_age = now_s - source_time
        if source_age < 0.0:
            return "range_source_future"
        if source_age > self.maximum_range_age_s:
            return "range_source_stale"

        if evaluated_time is not None:
            evaluation_age = now_s - evaluated_time
            if evaluation_age < 0.0:
                return "range_source_future"
            if data_age + evaluation_age > self.maximum_range_age_s:
                return "range_data_stale"

        if expected_frame_captured_at_s is not None:
            expected_age = now_s - expected_frame_captured_at_s
            if expected_age < 0.0:
                return "range_source_future"
            if expected_age > self.maximum_range_age_s:
                return "range_source_stale"
            if explicit_source_time and abs(source_time - expected_frame_captured_at_s) > 1.0e-6:
                return "range_frame_timestamp_mismatch"
        return None

    def _empty_range(self, reason: str) -> dict[str, object]:
        self._last_selection_metadata = (
            self._last_selection_metadata[0],
            None,
            self._last_selection_metadata[2],
        )
        self._last_reasons = tuple((*self._last_reasons, reason))
        return {
            "validity": None,
            "distance_m": None,
            "relative_bearing_deg": None,
            "absolute_bearing_deg": None,
            "confidence": None,
            "target_id": None,
        }

    def _home_vector(
        self,
        telemetry: object,
        home: object | None,
        vehicle: dict[str, object],
        *,
        explicit_bearing: float | None,
        explicit_distance: float | None,
    ) -> tuple[float | None, float | None]:
        bearing = _number(explicit_bearing, "home_bearing_deg", allow_nan=True)
        distance = _number(explicit_distance, "home_distance_m", allow_nan=True, minimum=0.0)
        source = home if home is not None else telemetry
        if bearing is None:
            bearing_raw = _read(source, "home_bearing_deg", "home_bearing", default=None)
            if bearing_raw is None and source is not telemetry:
                bearing_raw = _read(
                    telemetry, "home_bearing_deg", "home_bearing", default=None
                )
            bearing = _number(bearing_raw, "home_bearing_deg", allow_nan=True)
        if distance is None:
            distance_raw = _read(source, "home_distance_m", "distance_to_home_m", default=None)
            if distance_raw is None and source is not telemetry:
                distance_raw = _read(
                    telemetry, "home_distance_m", "distance_to_home_m", default=None
                )
            distance = _number(distance_raw, "home_distance_m", allow_nan=True, minimum=0.0)

        # If only home coordinates are available, derive a geodesic vector from
        # observed GPS.  This is a measurement transform, not simulator truth.
        home_lat_names = ("home_latitude_deg", "home_latitude")
        home_lon_names = ("home_longitude_deg", "home_longitude")
        if home is not None:
            home_lat_names += ("latitude_deg", "latitude", "lat")
            home_lon_names += ("longitude_deg", "longitude", "lon")
        home_lat = _number(
            _read(source, *home_lat_names, default=None),
            "home_latitude_deg",
            allow_nan=True,
            minimum=-90.0,
            maximum=90.0,
        )
        home_lon = _number(
            _read(source, *home_lon_names, default=None),
            "home_longitude_deg",
            allow_nan=True,
            minimum=-180.0,
            maximum=180.0,
        )
        if (_finite(home_lat) and not _finite(home_lon)) or (
            _finite(home_lon) and not _finite(home_lat)
        ):
            raise ValueError("home latitude and longitude must be supplied together")
        current_lat = vehicle["latitude_deg"]
        current_lon = vehicle["longitude_deg"]
        if (
            bearing is None
            and distance is None
            and _finite(home_lat)
            and _finite(home_lon)
            and _finite(current_lat)
            and _finite(current_lon)
        ):
            bearing, distance = self._great_circle_vector(
                float(current_lat),
                float(current_lon),
                float(home_lat),
                float(home_lon),
                float(vehicle["heading_deg"]),
            )
        if bearing is not None:
            bearing = _normalize_relative_bearing(bearing)
        return bearing, distance

    @staticmethod
    def _great_circle_vector(
        latitude_deg: float,
        longitude_deg: float,
        home_latitude_deg: float,
        home_longitude_deg: float,
        heading_deg: float,
    ) -> tuple[float, float]:
        lat1 = math.radians(latitude_deg)
        lat2 = math.radians(home_latitude_deg)
        dlat = lat2 - lat1
        dlon = math.radians(home_longitude_deg - longitude_deg)
        a = math.sin(dlat / 2.0) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2.0) ** 2
        distance = _EARTH_RADIUS_M * 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))
        absolute = math.degrees(
            math.atan2(
                math.sin(dlon) * math.cos(lat2),
                math.cos(lat1) * math.sin(lat2)
                - math.sin(lat1) * math.cos(lat2) * math.cos(dlon),
            )
        )
        return _normalize_relative_bearing(absolute - heading_deg) or 0.0, distance

    def _return_flag(
        self,
        telemetry: object,
        *,
        explicit: bool | None,
        home: object | None,
    ) -> bool:
        if explicit is not None:
            return bool(_boolean(explicit, "return_requested"))
        raw = _read(
            telemetry,
            "return_requested",
            "rtl_requested",
            "return_home",
            "mission_return_requested",
            default=None,
        )
        if raw is None and home is not None:
            raw = _read(
                home,
                "return_requested",
                "rtl_requested",
                "return_home",
                "mission_return_requested",
                default=None,
            )
        if raw is not None:
            return bool(_boolean(raw, "return_requested"))
        mode = _read(telemetry, "flight_mode", "mode", "mission_phase", "phase", default=None)
        mode_text = _enum_text(mode, "flight_mode", default=None)
        return mode_text in _RETURN_MODES if mode_text is not None else False

    def _obstacle_flag(
        self,
        avoidance: object | None,
        *,
        explicit: bool | None,
    ) -> tuple[bool, str | None]:
        if explicit is not None:
            value = _boolean(explicit, "obstacle_ahead")
            return bool(value), None
        if avoidance is None:
            return False, None
        if isinstance(avoidance, str) or getattr(avoidance, "value", None) is not None:
            return False, _state_text(avoidance)
        direct = _read(avoidance, "obstacle_ahead", "blocked", default=None)
        state = _state_text(_read(avoidance, "state", "risk_state", default=None))
        zones = _read(avoidance, "zones", default=())
        if direct is not None:
            return bool(_boolean(direct, "obstacle_ahead")), state
        if zones is not None:
            for zone in zones:
                zone_state = _state_text(_read(zone, "state", "risk_state", default=None))
                if zone_state in _AVOID_STATES:
                    return True, state or zone_state
        return state in _AVOID_STATES, state


# Alias names make the feed discoverable without coupling callers to one noun.
SimulatorAiPerceptionBridge = SimulatorPerceptionBridge
SimulatorPerceptionObservationFeed = SimulatorPerceptionBridge

__all__ = [
    "SimulatorAiPerceptionBridge",
    "SimulatorPerceptionBridge",
    "SimulatorPerceptionFeedResult",
    "SimulatorPerceptionObservationFeed",
]
