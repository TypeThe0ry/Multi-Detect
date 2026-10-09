"""ONNX Runtime adapter for the shared high-level OnboardAI contract.

The adapter is intentionally small: preprocessing is the canonical
``OnboardModelInput.feature_vector`` and the model emits one ``[1, 20]`` row:
9 semantic task logits, 7 navigation-action logits and 4 bounded movement
regressions.  The legacy ``[1, 11]`` navigation-only row remains readable for
older artifacts.  It has no MAVLink or
Mission access, so the exact same runner can be constructed in Gazebo/4060 and
on Jetson.  The caller still supplies the reviewed artifact identity to
``ModelBackedDecisionProvider``.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .onboard_agent import (
    ONBOARD_MODEL_FEATURE_NAMES,
    ONBOARD_MODEL_LEGACY_FEATURE_NAMES,
    OnboardEventKind,
    OnboardFlightAction,
    OnboardFlightPlan,
    OnboardModelInput,
    OnboardModelOutput,
    OnboardResponse,
    OnboardTaskAction,
)

ONBOARD_MODEL_RESPONSE_ORDER: tuple[OnboardResponse, ...] = (
    OnboardResponse.EXECUTE,
    OnboardResponse.REPLAN,
    OnboardResponse.RETURN,
    OnboardResponse.HOLD,
    OnboardResponse.LAND,
)
ONBOARD_MODEL_ACTION_ORDER: tuple[OnboardFlightAction, ...] = (
    OnboardFlightAction.HOLD_POSITION,
    OnboardFlightAction.MOVE_RELATIVE,
    OnboardFlightAction.GOTO_RELATIVE,
    OnboardFlightAction.SEARCH,
    OnboardFlightAction.FOLLOW_TARGET,
    OnboardFlightAction.RETURN_HOME,
    OnboardFlightAction.LAND,
)
ONBOARD_MODEL_TASK_ACTION_ORDER: tuple[OnboardTaskAction, ...] = tuple(OnboardTaskAction)
ONBOARD_MODEL_MAX_RELATIVE_ALTITUDE_M = 120.0
ONBOARD_MODEL_MAX_SPEED_MPS = 50.0


_TASK_FLIGHT_COMPATIBILITY: dict[OnboardTaskAction, frozenset[OnboardFlightAction]] = {
    OnboardTaskAction.TAKEOFF: frozenset({OnboardFlightAction.GOTO_RELATIVE}),
    OnboardTaskAction.SEARCH: frozenset(
        {
            OnboardFlightAction.MOVE_RELATIVE,
            OnboardFlightAction.GOTO_RELATIVE,
            OnboardFlightAction.SEARCH,
            OnboardFlightAction.FOLLOW_TARGET,
        }
    ),
    OnboardTaskAction.INSPECT_TARGET: frozenset(
        {OnboardFlightAction.GOTO_RELATIVE, OnboardFlightAction.FOLLOW_TARGET}
    ),
    OnboardTaskAction.MATCH_TARGET: frozenset(
        {OnboardFlightAction.GOTO_RELATIVE, OnboardFlightAction.FOLLOW_TARGET}
    ),
    OnboardTaskAction.RECORD_LOCATION: frozenset({OnboardFlightAction.RETURN_HOME}),
    OnboardTaskAction.RETURN_HOME: frozenset({OnboardFlightAction.RETURN_HOME}),
    OnboardTaskAction.REPORT: frozenset(
        {OnboardFlightAction.HOLD_POSITION, OnboardFlightAction.LAND}
    ),
    OnboardTaskAction.HOLD: frozenset({OnboardFlightAction.HOLD_POSITION}),
    OnboardTaskAction.LAND: frozenset({OnboardFlightAction.LAND}),
}

# The perception/tracker phase is an observed lifecycle contract.  It may
# constrain the model's semantic head, but it never emits a MAVLink command.
# This prevents an otherwise valid RETURN_HOME logit from skipping the
# required RECORD_LOCATION receipt when the detector has supplied a qualified
# location phase.
_TASK_PHASE_COMPATIBILITY: dict[OnboardTaskAction, frozenset[OnboardTaskAction]] = {
    # The detector/tracker phase is an observed lifecycle boundary, not a
    # suggestion.  Once live telemetry has promoted TAKEOFF to SEARCH, a
    # noisy model head must not be allowed to jump back to TAKEOFF and invoke
    # an airborne safety-mode transition.  Keep the semantic head learned,
    # but restrict it to the transitions the task state machine can accept.
    OnboardTaskAction.TAKEOFF: frozenset({OnboardTaskAction.TAKEOFF, OnboardTaskAction.SEARCH}),
    OnboardTaskAction.SEARCH: frozenset(
        {
            OnboardTaskAction.SEARCH,
            OnboardTaskAction.INSPECT_TARGET,
            OnboardTaskAction.MATCH_TARGET,
        }
    ),
    OnboardTaskAction.INSPECT_TARGET: frozenset(
        {
            OnboardTaskAction.INSPECT_TARGET,
            OnboardTaskAction.MATCH_TARGET,
            OnboardTaskAction.SEARCH,
        }
    ),
    OnboardTaskAction.MATCH_TARGET: frozenset(
        {OnboardTaskAction.MATCH_TARGET, OnboardTaskAction.RECORD_LOCATION}
    ),
    OnboardTaskAction.RECORD_LOCATION: frozenset({OnboardTaskAction.RECORD_LOCATION}),
    OnboardTaskAction.RETURN_HOME: frozenset(
        {OnboardTaskAction.RETURN_HOME, OnboardTaskAction.REPORT}
    ),
    OnboardTaskAction.REPORT: frozenset({OnboardTaskAction.REPORT}),
    # HOLD and LAND are terminal/safety phases.  They are intentionally
    # explicit here so a non-health event cannot make the learned head jump
    # back into an active search or target-follow action.
    OnboardTaskAction.HOLD: frozenset({OnboardTaskAction.HOLD}),
    OnboardTaskAction.LAND: frozenset({OnboardTaskAction.LAND}),
}


def is_task_flight_compatible(
    task_action: OnboardTaskAction | None,
    flight_action: OnboardFlightAction | None,
) -> bool:
    """Return whether one semantic task and flight action may be paired.

    This is the same compatibility table used by :class:`OnboardOnnxModelRunner`.
    Evaluation and evidence code must call this helper instead of treating the
    task and flight heads as two independent classifications.
    """

    if task_action is None or flight_action is None:
        return False
    return flight_action in _TASK_FLIGHT_COMPATIBILITY.get(task_action, frozenset())


def allowed_task_actions_for_event(
    event_kind: OnboardEventKind,
    phase: OnboardTaskAction | None,
) -> frozenset[OnboardTaskAction] | None:
    """Return the lifecycle/safety task set used to mask one model result."""

    allowed = _TASK_PHASE_COMPATIBILITY.get(phase) if phase is not None else None
    if event_kind is OnboardEventKind.BATTERY_LOW:
        return frozenset({OnboardTaskAction.RETURN_HOME, OnboardTaskAction.LAND})
    if event_kind in {
        OnboardEventKind.GPS_DEGRADED,
        OnboardEventKind.OBSTACLE_RISK,
        OnboardEventKind.INFERENCE_FAULT,
    }:
        return frozenset(
            {OnboardTaskAction.HOLD, OnboardTaskAction.RETURN_HOME, OnboardTaskAction.LAND}
        )
    if event_kind is OnboardEventKind.MISSION_COMPLETE:
        if phase is OnboardTaskAction.RECORD_LOCATION:
            return frozenset({OnboardTaskAction.RETURN_HOME})
        if phase is OnboardTaskAction.RETURN_HOME:
            return frozenset({OnboardTaskAction.REPORT})
    return allowed


class OnboardOnnxModelError(RuntimeError):
    """Raised when an OnboardAI ONNX session violates its IO contract."""


def decode_onboard_policy_output(raw: object, model_input: OnboardModelInput) -> OnboardModelOutput:
    """Decode one shared policy result; current and history runners use the same rules."""

    if not isinstance(model_input, OnboardModelInput):
        raise TypeError("model_input must be an OnboardModelInput")
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - dependency-gated runtime path
        raise OnboardOnnxModelError("numpy is required for OnboardAI ONNX inference") from exc

    if not isinstance(raw, (list, tuple)) or len(raw) != 1:
        raise OnboardOnnxModelError("OnboardAI ONNX session returned an invalid output list")
    logits = np.asarray(raw[0], dtype=np.float32)
    legacy_width = len(ONBOARD_MODEL_ACTION_ORDER) + 4
    task_width = len(ONBOARD_MODEL_TASK_ACTION_ORDER) + legacy_width
    if logits.shape not in {(1, legacy_width), (1, task_width)}:
        raise OnboardOnnxModelError(
            "OnboardAI ONNX output must have shape "
            f"[1, {task_width}] (or legacy [1, {legacy_width}]), got {logits.shape}"
        )
    if not np.isfinite(logits).all():
        raise OnboardOnnxModelError("OnboardAI ONNX output contains non-finite logits")
    has_task_head = logits.shape[1] == task_width
    task_action = None
    task_confidence = 1.0
    task_phase_masked = False
    if has_task_head:
        task_logits = logits[0, : len(ONBOARD_MODEL_TASK_ACTION_ORDER)]
        # Health events are allowed to interrupt a normal perception
        # phase.  The event-specific mask prevents a lifecycle phase from
        # hiding HOLD/RETURN/LAND logits, while the adapter-owned safety
        # supervisor remains the final authority.
        allowed_task_actions = allowed_task_actions_for_event(
            model_input.event.kind,
            model_input.event.task_phase,
        )
        if allowed_task_actions is not None:
            allowed_task_indices = tuple(
                index
                for index, candidate in enumerate(ONBOARD_MODEL_TASK_ACTION_ORDER)
                if candidate in allowed_task_actions
            )
            raw_task_index = int(np.argmax(task_logits))
            if raw_task_index not in allowed_task_indices:
                task_phase_masked = True
                masked_task_logits = np.full_like(task_logits, -np.inf)
                masked_task_logits[list(allowed_task_indices)] = task_logits[
                    list(allowed_task_indices)
                ]
                task_logits = masked_task_logits
        task_shifted = task_logits - np.max(task_logits)
        task_probabilities = np.exp(task_shifted)
        task_probabilities /= np.sum(task_probabilities)
        task_index = int(np.argmax(task_probabilities))
        task_action = ONBOARD_MODEL_TASK_ACTION_ORDER[task_index]
        task_confidence = float(task_probabilities[task_index])
        action_start = len(ONBOARD_MODEL_TASK_ACTION_ORDER)
    else:
        action_start = 0
    action_logits = logits[0, action_start : action_start + len(ONBOARD_MODEL_ACTION_ORDER)]
    compatibility_masked = False
    if task_action is not None:
        allowed_actions = _TASK_FLIGHT_COMPATIBILITY[task_action]
        allowed_indices = tuple(
            index
            for index, candidate in enumerate(ONBOARD_MODEL_ACTION_ORDER)
            if candidate in allowed_actions
        )
        if not allowed_indices:  # pragma: no cover - contract table guard
            raise OnboardOnnxModelError(
                f"OnboardAI task action has no compatible flight action: {task_action.value}"
            )
        raw_index = int(np.argmax(action_logits))
        if raw_index not in allowed_indices:
            compatibility_masked = True
            masked_logits = np.full_like(action_logits, -np.inf)
            masked_logits[list(allowed_indices)] = action_logits[list(allowed_indices)]
            action_logits = masked_logits
    shifted = action_logits - np.max(action_logits)
    probabilities = np.exp(shifted)
    probabilities /= np.sum(probabilities)
    index = int(np.argmax(probabilities))
    action = ONBOARD_MODEL_ACTION_ORDER[index]
    response = {
        OnboardFlightAction.HOLD_POSITION: OnboardResponse.HOLD,
        OnboardFlightAction.RETURN_HOME: OnboardResponse.RETURN,
        OnboardFlightAction.LAND: OnboardResponse.LAND,
    }.get(action, OnboardResponse.REPLAN)
    regression_start = action_start + len(ONBOARD_MODEL_ACTION_ORDER)
    north_offset_m = float(np.tanh(logits[0, regression_start]) * 500.0)
    east_offset_m = float(np.tanh(logits[0, regression_start + 1]) * 500.0)
    altitude_m = min(
        ONBOARD_MODEL_MAX_RELATIVE_ALTITUDE_M,
        float(
            (1.0 / (1.0 + np.exp(-logits[0, regression_start + 2])))
            * ONBOARD_MODEL_MAX_RELATIVE_ALTITUDE_M
        ),
    )
    speed_mps = float(
        (1.0 / (1.0 + np.exp(-logits[0, regression_start + 3]))) * ONBOARD_MODEL_MAX_SPEED_MPS
    )
    if action in {
        OnboardFlightAction.HOLD_POSITION,
        OnboardFlightAction.RETURN_HOME,
        OnboardFlightAction.LAND,
    }:
        north_offset_m = 0.0
        east_offset_m = 0.0
    confidence = float(probabilities[index] * task_confidence)
    reason = f"model_plan_{action.value}"
    if task_action is not None:
        reason = f"model_task_{task_action.value}_plan_{action.value}"
    if compatibility_masked:
        reason += "_compatibility_masked"
    if task_phase_masked:
        reason += "_task_phase_masked"
    return OnboardModelOutput(
        response=response,
        reason=reason,
        confidence=confidence,
        flight_plan=OnboardFlightPlan(
            action=action,
            north_offset_m=north_offset_m,
            east_offset_m=east_offset_m,
            altitude_m=altitude_m,
            speed_mps=speed_mps,
        ),
        task_action=task_action,
    )


class OnboardOnnxModelRunner:
    """Run one validated ONNX high-level policy session."""

    __slots__ = ("session", "input_name", "output_name", "input_feature_width")

    def __init__(self, session: Any) -> None:
        if not callable(getattr(session, "run", None)):
            raise TypeError("session must expose a callable run method")
        inputs = tuple(getattr(session, "get_inputs", lambda: ())())
        outputs = tuple(getattr(session, "get_outputs", lambda: ())())
        if len(inputs) != 1 or len(outputs) != 1:
            raise OnboardOnnxModelError(
                "OnboardAI ONNX model must have exactly one input and output"
            )
        feature_width = len(ONBOARD_MODEL_FEATURE_NAMES)
        input_shape = getattr(inputs[0], "shape", None)
        if input_shape is not None and len(input_shape) == 2:
            width = input_shape[1]
            if isinstance(width, int) and width not in {
                len(ONBOARD_MODEL_FEATURE_NAMES),
                len(ONBOARD_MODEL_LEGACY_FEATURE_NAMES),
            }:
                raise OnboardOnnxModelError(
                    "OnboardAI ONNX input feature width does not match the shared contract: "
                    f"expected {len(ONBOARD_MODEL_FEATURE_NAMES)} or "
                    f"{len(ONBOARD_MODEL_LEGACY_FEATURE_NAMES)}, got {width}"
                )
            if isinstance(width, int):
                feature_width = width
        self.session = session
        self.input_name = str(getattr(inputs[0], "name", ""))
        self.output_name = str(getattr(outputs[0], "name", ""))
        if not self.input_name or not self.output_name:
            raise OnboardOnnxModelError("OnboardAI ONNX model input/output names are required")
        self.input_feature_width = feature_width

    @classmethod
    def from_path(
        cls,
        model_path: str | Path,
        *,
        providers: Sequence[str] | None = None,
        trt_engine_cache_path: Path | None = None,
    ) -> OnboardOnnxModelRunner:
        try:
            import onnxruntime as ort
        except ImportError as exc:  # pragma: no cover - dependency-gated runtime path
            raise OnboardOnnxModelError(
                "onnxruntime is required for OnboardAI ONNX inference"
            ) from exc
        from ..inference_provider import create_onnxruntime_session

        session, _selection = create_onnxruntime_session(
            ort,
            model_path,
            providers=providers,
            trt_engine_cache_path=trt_engine_cache_path,
        )
        return cls(session)

    def infer(self, model_input: OnboardModelInput) -> OnboardModelOutput:
        if not isinstance(model_input, OnboardModelInput):
            raise TypeError("model_input must be an OnboardModelInput")
        try:
            import numpy as np
        except ImportError as exc:  # pragma: no cover - dependency-gated runtime path
            raise OnboardOnnxModelError("numpy is required for OnboardAI ONNX inference") from exc

        feature_vector = (
            model_input.legacy_feature_vector()
            if self.input_feature_width == len(ONBOARD_MODEL_LEGACY_FEATURE_NAMES)
            else model_input.feature_vector()
        )
        vector = np.asarray([feature_vector], dtype=np.float32)
        raw = self.session.run([self.output_name], {self.input_name: vector})
        return decode_onboard_policy_output(raw, model_input)


__all__ = [
    "ONBOARD_MODEL_RESPONSE_ORDER",
    "ONBOARD_MODEL_ACTION_ORDER",
    "ONBOARD_MODEL_TASK_ACTION_ORDER",
    "ONBOARD_MODEL_MAX_RELATIVE_ALTITUDE_M",
    "ONBOARD_MODEL_MAX_SPEED_MPS",
    "OnboardOnnxModelError",
    "OnboardOnnxModelRunner",
    "decode_onboard_policy_output",
]
