"""Strict static ONNX adapter for a session-bound eight-frame policy window."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from threading import RLock
from typing import Any

from .onboard_agent import ONBOARD_MODEL_FEATURE_NAMES, OnboardModelInput, OnboardModelOutput
from .onboard_history import (
    ONBOARD_HISTORY_FEATURE_WIDTH,
    ONBOARD_HISTORY_LENGTH,
    ONBOARD_HISTORY_MAX_GAP_S,
    ONBOARD_HISTORY_OUTPUT_WIDTH,
    OnboardHistoryBuffer,
    OnboardHistoryError,
    OnboardInferenceScope,
)
from .serialization import canonical_sha256, require_sha256, strict_json_loads


def onboard_history_metadata() -> dict[str, str]:
    """Exporters must bind the exact current feature order and tensor semantics."""

    from .onboard_onnx import ONBOARD_MODEL_ACTION_ORDER, ONBOARD_MODEL_TASK_ACTION_ORDER

    return {
        "contract": "multidetect.onboard-history-policy-v1",
        "history_steps": str(ONBOARD_HISTORY_LENGTH),
        "feature_names": json.dumps(list(ONBOARD_MODEL_FEATURE_NAMES)),
        "task_action_order": json.dumps([item.value for item in ONBOARD_MODEL_TASK_ACTION_ORDER]),
        "flight_action_order": json.dumps([item.value for item in ONBOARD_MODEL_ACTION_ORDER]),
        "dt_clip_s": "30",
        "alignment": "right_current_last_zero_padding",
        "valid_mask_semantics": "one_real_zero_padding",
        "dt_s_semantics": "raw_seconds_zero_padding_and_origin",
        "reset_mask_semantics": "one_session_origin_zero_padding",
    }


class OnboardHistoryOnnxModelRunner:
    """Same output decoder and ORT factory as the single-frame shared runner.

    ``infer`` intentionally rejects unscoped use. Inference uses an explicit
    full window, never backend recurrent state across calls. Failed model
    execution or decoding leaves the prior committed history unchanged.
    """

    __slots__ = ("session", "history", "_lock", "_last_inference_receipt")

    def __init__(self, session: Any, *, max_gap_s: float = ONBOARD_HISTORY_MAX_GAP_S) -> None:
        self.history = OnboardHistoryBuffer(max_gap_s=max_gap_s)
        expected_inputs = {
            "features": [1, ONBOARD_HISTORY_LENGTH, ONBOARD_HISTORY_FEATURE_WIDTH],
            "valid_mask": [1, ONBOARD_HISTORY_LENGTH],
            "dt_s": [1, ONBOARD_HISTORY_LENGTH],
            "reset_mask": [1, ONBOARD_HISTORY_LENGTH],
        }
        inputs = session.get_inputs()
        outputs = session.get_outputs()
        if len(inputs) != 4 or {node.name for node in inputs} != set(expected_inputs):
            raise OnboardHistoryError("history ONNX input names/count are invalid")
        for node in inputs:
            self._validate_node(node, expected_inputs[node.name])
        if len(outputs) != 1 or outputs[0].name != "policy_output":
            raise OnboardHistoryError("history ONNX output must be policy_output")
        self._validate_node(outputs[0], [1, ONBOARD_HISTORY_OUTPUT_WIDTH])
        metadata = session.get_modelmeta().custom_metadata_map
        for key, value in onboard_history_metadata().items():
            actual = metadata.get(key)
            if key in {"feature_names", "task_action_order", "flight_action_order"}:
                if not isinstance(actual, str) or strict_json_loads(actual) != json.loads(value):
                    raise OnboardHistoryError(f"history ONNX metadata mismatch: {key}")
            elif actual != value:
                raise OnboardHistoryError(f"history ONNX metadata mismatch: {key}")
        require_sha256(metadata.get("parent_model_sha256"), "parent_model_sha256")
        if metadata.get("temporal_training_performed") not in {"true", "false"}:
            raise OnboardHistoryError("history ONNX temporal training provenance is missing")
        if metadata.get("candidate_promoted") != "false":
            raise OnboardHistoryError("history ONNX candidate must remain unpromoted")
        self.session = session
        self._lock = RLock()
        self._last_inference_receipt: dict[str, object] | None = None

    @staticmethod
    def _validate_node(node: Any, expected_shape: list[int]) -> None:
        if (
            node.type != "tensor(float)"
            or not isinstance(node.shape, (list, tuple))
            or list(node.shape) != expected_shape
            or any(type(dimension) is not int for dimension in node.shape)
        ):
            raise OnboardHistoryError(
                f"history ONNX {node.name} must have static float32 shape {expected_shape}"
            )

    @classmethod
    def from_path(
        cls,
        path: str | Path,
        *,
        providers: Sequence[str] | None = None,
        trt_engine_cache_path: str | Path | None = None,
        max_gap_s: float = ONBOARD_HISTORY_MAX_GAP_S,
    ) -> OnboardHistoryOnnxModelRunner:
        try:
            import onnxruntime as ort
        except ImportError as exc:  # pragma: no cover - optional runtime dependency
            raise OnboardHistoryError("onnxruntime is required for history inference") from exc
        from ..inference_provider import create_onnxruntime_session

        session, _ = create_onnxruntime_session(
            ort,
            path,
            providers=providers,
            trt_engine_cache_path=trt_engine_cache_path,
        )
        return cls(session, max_gap_s=max_gap_s)

    @property
    def last_inference_receipt(self) -> dict[str, object] | None:
        with self._lock:
            return (
                None if self._last_inference_receipt is None else dict(self._last_inference_receipt)
            )

    def infer(self, model_input: OnboardModelInput) -> OnboardModelOutput:
        raise OnboardHistoryError("history inference requires an explicit session scope")

    def reset_history(self, *, reason: str, scope: OnboardInferenceScope | None = None) -> None:
        with self._lock:
            self.history.reset(reason=reason, scope=scope)
            self._last_inference_receipt = None

    def observe_scoped(self, model_input: OnboardModelInput, scope: OnboardInferenceScope) -> bool:
        """Collect a suppressed observation without policy inference or Mission changes."""

        with self._lock:
            window = self.history.prepare(model_input, scope)
            changed = window.entries != self.history.entries
            self.history.commit(window)
            return changed

    def infer_scoped(
        self, model_input: OnboardModelInput, scope: OnboardInferenceScope
    ) -> OnboardModelOutput:
        import numpy as np

        from .onboard_onnx import decode_onboard_policy_output

        with self._lock:
            window = self.history.prepare(model_input, scope)
            tensors = {
                "features": np.asarray([window.features], dtype=np.float32),
                "valid_mask": np.asarray([window.valid_mask], dtype=np.float32),
                "dt_s": np.asarray([window.dt_s], dtype=np.float32),
                "reset_mask": np.asarray([window.reset_mask], dtype=np.float32),
            }
            if not all(np.isfinite(tensor).all() for tensor in tensors.values()):
                raise OnboardHistoryError("history tensors contain non-finite float32 values")
            raw = self.session.run(["policy_output"], tensors)
            if (
                not isinstance(raw, (list, tuple))
                or len(raw) != 1
                or not isinstance(raw[0], np.ndarray)
                or raw[0].shape != (1, ONBOARD_HISTORY_OUTPUT_WIDTH)
                or raw[0].dtype != np.float32
                or not np.isfinite(raw[0]).all()
            ):
                raise OnboardHistoryError("history ONNX output must be finite float32 [1,20]")
            output = decode_onboard_policy_output(raw, model_input)
            self.history.commit(window)
            self._last_inference_receipt = {
                "contract": "multidetect.onboard-history-inference-v1",
                "scope_sha256": scope.canonical_hash,
                "window_sha256": window.canonical_hash,
                "current_input_sha256": canonical_sha256(model_input.to_mapping()),
                "event_count": len(window.entries),
                "truth_free": True,
                "training_ready": False,
            }
            return output
