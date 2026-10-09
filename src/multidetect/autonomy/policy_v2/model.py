"""Small, safety-oriented baseline policy for policy-v2.

The model intentionally has a boring tensor contract.  A caller supplies a
history of projected state features ``(B, 16, 128)``, the search memory as
``(B, 5, 16, 16)``, and an optional four element action mask.  The exported
ONNX graph has the same three inputs and four outputs.  Conversion to the
wire-level :class:`PolicyActionV2` is kept outside the graph so malformed
inputs and outputs can fail closed to HOLD.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

try:  # Torch is an optional dependency of the repository.
    import torch
    from torch import Tensor, nn
except ImportError:  # pragma: no cover - exercised by environments without torch
    torch = None  # type: ignore[assignment]
    Tensor = Any  # type: ignore[misc,assignment]
    nn = Any  # type: ignore[assignment]

from .contracts import PolicyActionV2, PolicyContractError, StructuredSceneState

ACTION_KINDS = ("translate", "turn", "hold", "stop")
STATE_FEATURE_DIM = 128
HISTORY_LENGTH = 16
MEMORY_CHANNELS = 5
MEMORY_SIZE = 16
_BOUNDS = ((-2.0, 8.0), (-6.0, 6.0), (-2.0, 2.0), (-90.0, 90.0), (0.2, 2.0))


def _require_torch() -> None:
    if torch is None:
        raise RuntimeError("policy-v2 model requires PyTorch")


def _numeric_values(value: object, output: list[float]) -> None:
    """Collect finite numeric leaves while deliberately ignoring text labels."""
    if isinstance(value, bool):
        output.append(float(value))
    elif isinstance(value, (int, float)):
        number = float(value)
        if math.isfinite(number):
            output.append(number)
    elif isinstance(value, Mapping):
        for child in value.values():
            _numeric_values(child, output)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _numeric_values(child, output)


def state_to_vector(state: StructuredSceneState | Mapping[str, Any]) -> list[float]:
    """Project one structured state into the frozen 128-float model input.

    Numeric leaves are traversed in the contract's insertion order.  Text
    fields (including requested labels and frame ids) never become model
    inputs.  Padding and truncation keep this helper deterministic as the
    state contract gains optional blocks.
    """
    raw = state.to_mapping() if isinstance(state, StructuredSceneState) else state
    if not isinstance(raw, Mapping):
        raise PolicyContractError("state must be a StructuredSceneState or mapping")
    values: list[float] = []
    # Exclude schema/hash/frame and the truth marker.  History is already a
    # sequence of prior state blocks and is included after the current blocks.
    for key in (
        "valid_mask",
        "reset_mask",
        "health",
        "pose",
        "task",
        "tracks",
        "scene",
        "memory",
        "event",
    ):
        if key in raw and key != "memory":
            _numeric_values(raw[key], values)
    if len(values) < STATE_FEATURE_DIM:
        values.extend([0.0] * (STATE_FEATURE_DIM - len(values)))
    return values[:STATE_FEATURE_DIM]


def states_to_tensors(
    states: StructuredSceneState | Sequence[StructuredSceneState],
    *,
    device: Any | None = None,
) -> tuple[Tensor, Tensor]:
    """Convert a state or state history to model tensors.

    A single state uses its fixed 16-tick ``history`` when present; callers
    passing a sequence get one sequence per batch item.  Memory is taken from
    the last state and transposed from HWC to CHW.
    """
    _require_torch()
    if isinstance(states, StructuredSceneState):
        raw_history = [item for item in states.history if isinstance(item, Mapping)]
        history: list[StructuredSceneState | Mapping[str, Any]] = raw_history + [states]
        history = history[-HISTORY_LENGTH:]
        while len(history) < HISTORY_LENGTH:
            history.insert(0, {})
        batch = [history]
        last = states
    else:
        sequence = list(states)
        if not sequence or not all(isinstance(item, StructuredSceneState) for item in sequence):
            raise PolicyContractError("states sequence must contain StructuredSceneState values")
        batch = [sequence[-HISTORY_LENGTH:]]
        while len(batch[0]) < HISTORY_LENGTH:
            batch[0].insert(0, {})
        last = sequence[-1]
    state_data = [[state_to_vector(item) for item in row] for row in batch]
    state_tensor = torch.tensor(state_data, dtype=torch.float32, device=device)
    memory = torch.tensor(last.memory, dtype=torch.float32, device=device)
    memory = memory.permute(2, 0, 1).unsqueeze(0)
    return state_tensor, memory


if torch is not None:

    class PrimitivePolicy(nn.Module):
        """Masked two-layer GRU policy baseline.

        ``forward`` returns tensors suitable for supervised training and ONNX
        export.  ``act`` is the fail-closed runtime boundary and returns a
        validated ``PolicyActionV2``.
        """

        def __init__(
            self,
            *,
            state_dim: int = STATE_FEATURE_DIM,
            hidden_size: int = 256,
            history_length: int = HISTORY_LENGTH,
        ) -> None:
            super().__init__()
            if state_dim <= 0 or history_length != HISTORY_LENGTH:
                raise ValueError("state_dim must be positive and history length is fixed at 16")
            self.state_dim = int(state_dim)
            self.hidden_size = int(hidden_size)
            self.history_length = int(history_length)
            self.state_projection = nn.Sequential(
                nn.Linear(self.state_dim, 128), nn.LayerNorm(128), nn.SiLU()
            )
            self.gru = nn.GRU(128, self.hidden_size, num_layers=2, batch_first=True)
            self.memory_cnn = nn.Sequential(
                nn.Conv2d(MEMORY_CHANNELS, 16, 3, padding=1),
                nn.SiLU(),
                nn.Conv2d(16, 32, 3, stride=2, padding=1),
                nn.SiLU(),
                nn.AdaptiveAvgPool2d((1, 1)),
                nn.Flatten(),
                nn.Linear(32, 64),
                nn.SiLU(),
            )
            self.fusion = nn.Sequential(
                nn.Linear(self.hidden_size + 64, 256), nn.LayerNorm(256), nn.SiLU()
            )
            self.kind_head = nn.Linear(256, 4)
            self.continuous_head = nn.Linear(256, 5)
            self.value_head = nn.Linear(256, 1)
            self.uncertainty_head = nn.Linear(256, 1)

        def forward(
            self,
            state_history: Tensor,
            memory: Tensor,
            action_mask: Tensor | None = None,
        ) -> tuple[Tensor, Tensor, Tensor, Tensor]:
            if state_history.ndim != 3 or state_history.shape[-1] != self.state_dim:
                raise ValueError(f"state_history must be (B,T,{self.state_dim})")
            if memory.ndim != 4 or memory.shape[1:] != (MEMORY_CHANNELS, MEMORY_SIZE, MEMORY_SIZE):
                raise ValueError("memory must be (B,5,16,16)")
            finite = torch.isfinite(state_history).all(dim=(1, 2)) & torch.isfinite(memory).all(
                dim=(1, 2, 3)
            )
            # Replace invalid values before neural layers; act() subsequently
            # converts the corresponding row to HOLD.  This keeps ONNX free of
            # NaNs while preserving the fail-closed runtime behavior.
            clean_state = torch.nan_to_num(state_history, nan=0.0, posinf=0.0, neginf=0.0)
            clean_memory = torch.nan_to_num(memory, nan=0.0, posinf=0.0, neginf=0.0)
            projected = self.state_projection(clean_state)
            sequence, _ = self.gru(projected)
            fused = self.fusion(torch.cat((sequence[:, -1], self.memory_cnn(clean_memory)), dim=1))
            logits = self.kind_head(fused)
            if action_mask is not None:
                mask = action_mask.to(dtype=logits.dtype)
                if mask.ndim != 2 or mask.shape[-1] != 4:
                    raise ValueError("action_mask must be (B,4)")
                # All-false rows get HOLD as their only admissible fallback.
                any_allowed = mask.sum(dim=1, keepdim=True) > 0
                fallback = torch.zeros_like(mask)
                fallback[:, 2:3] = 1.0
                mask = torch.where(any_allowed, mask, fallback)
                logits = logits.masked_fill(mask <= 0.0, -1.0e9)
            # tanh bounds are kept explicit in the graph for TensorRT.
            tanh = torch.tanh(self.continuous_head(fused))
            mins = tanh.new_tensor([b[0] for b in _BOUNDS])
            maxs = tanh.new_tensor([b[1] for b in _BOUNDS])
            continuous = mins + (tanh + 1.0) * 0.5 * (maxs - mins)
            value = self.value_head(fused)
            uncertainty = torch.sigmoid(self.uncertainty_head(fused))
            # ``finite`` is not returned as an additional ONNX output to keep
            # the contract stable; invalid rows are represented by HOLD logits.
            invalid = ~finite
            if bool(invalid.any()):
                hold = torch.full_like(logits, -1.0e9)
                hold[:, 2] = 0.0
                logits = torch.where(invalid[:, None], hold, logits)
            return logits, continuous, value, uncertainty

        @torch.no_grad()
        def act(
            self,
            state_history: Tensor | StructuredSceneState,
            memory: Tensor | None = None,
            action_mask: Tensor | Sequence[bool] | None = None,
        ) -> PolicyActionV2:
            self.eval()
            try:
                if isinstance(state_history, StructuredSceneState):
                    state_history, memory = states_to_tensors(
                        state_history, device=next(self.parameters()).device
                    )
                if memory is None:
                    raise PolicyContractError("memory is required with tensor state_history")
                if state_history.ndim == 2:
                    state_history = state_history.unsqueeze(0)
                if memory.ndim == 3:
                    memory = memory.unsqueeze(0)
                if action_mask is None:
                    mask = torch.ones((state_history.shape[0], 4), device=state_history.device)
                else:
                    mask = torch.as_tensor(
                        action_mask, dtype=torch.float32, device=state_history.device
                    )
                    if mask.ndim == 1:
                        mask = mask.unsqueeze(0)
                if mask.ndim != 2 or mask.shape[-1] != 4 or not bool(torch.isfinite(mask).all()):
                    return PolicyActionV2("hold", seconds=0.2)
                if not bool(((mask == 0.0) | (mask == 1.0)).all()):
                    return PolicyActionV2("hold", seconds=0.2)
                valid = bool(torch.isfinite(state_history).all() and torch.isfinite(memory).all())
                if not valid:
                    return PolicyActionV2("hold", seconds=0.2, action_mask=(True, True, True, True))
                logits, continuous, _value, _uncertainty = self(state_history, memory, mask)
                kind_index = int(torch.argmax(logits[0]).item())
                allowed = [bool(v) for v in mask[0].detach().cpu().tolist()]
                if not any(allowed):
                    kind_index = 2
                elif not allowed[kind_index]:
                    kind_index = (
                        2 if allowed[2] else next(i for i, val in enumerate(allowed) if val)
                    )
                values = [float(v) for v in continuous[0].detach().cpu().tolist()]
                kwargs = dict(
                    forward_m=values[0] if kind_index == 0 else 0.0,
                    right_m=values[1] if kind_index == 0 else 0.0,
                    up_m=values[2] if kind_index == 0 else 0.0,
                    yaw_delta_deg=values[3] if kind_index == 1 else 0.0,
                    seconds=values[4] if kind_index == 2 else 0.0,
                    action_mask=tuple(allowed) if any(allowed) else (True, True, True, True),
                )
                return PolicyActionV2(ACTION_KINDS[kind_index], **kwargs)
            except (PolicyContractError, ValueError, RuntimeError, StopIteration, IndexError):
                return PolicyActionV2("hold", seconds=0.2, action_mask=(True, True, True, True))

else:

    class PrimitivePolicy:  # pragma: no cover - import-only fallback
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            _require_torch()


def export_onnx(
    model: PrimitivePolicy,
    path: str | Path,
    *,
    opset: int = 17,
    state_dim: int | None = None,
    history_length: int = HISTORY_LENGTH,
) -> Path:
    """Export the fixed policy I/O contract (opset 17)."""
    _require_torch()
    if opset != 17:
        raise ValueError("policy-v2 ONNX contract is frozen at opset 17")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    model.eval()
    dim = int(state_dim or model.state_dim)
    sample = (
        torch.zeros(1, history_length, dim),
        torch.zeros(1, MEMORY_CHANNELS, MEMORY_SIZE, MEMORY_SIZE),
        torch.ones(1, 4),
    )
    torch.onnx.export(
        model,
        sample,
        str(target),
        opset_version=opset,
        input_names=["state_history", "memory", "action_mask"],
        output_names=["kind_logits", "continuous", "value", "uncertainty"],
        dynamic_axes={
            "state_history": {0: "batch"},
            "memory": {0: "batch"},
            "action_mask": {0: "batch"},
            "kind_logits": {0: "batch"},
            "continuous": {0: "batch"},
            "value": {0: "batch"},
            "uncertainty": {0: "batch"},
        },
    )
    return target


class ONNXPolicyRunner:
    """Fail-closed onnxruntime adapter returning ``PolicyActionV2``."""

    def __init__(self, path: str | Path, *, providers: Sequence[str] | None = None) -> None:
        try:
            import numpy as np
            import onnxruntime as ort
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("onnxruntime and numpy are required") from exc
        self._np = np
        selected = list(providers or ["CUDAExecutionProvider", "CPUExecutionProvider"])
        available = ort.get_available_providers()
        selected = [provider for provider in selected if provider in available]
        if not selected:
            selected = ["CPUExecutionProvider"]
        self.session = ort.InferenceSession(str(path), providers=selected)

    def act(
        self,
        state_history: Any,
        memory: Any | None = None,
        action_mask: Any | None = None,
    ) -> PolicyActionV2:
        np = self._np
        try:
            if isinstance(state_history, StructuredSceneState):
                state_history, memory = states_to_tensors(state_history)
                state_history = state_history.detach().cpu().numpy()
                memory = memory.detach().cpu().numpy()
            if memory is None:
                raise ValueError("memory is required with tensor state_history")
            state = np.asarray(state_history, dtype=np.float32)
            mem = np.asarray(memory, dtype=np.float32)
            if state.ndim == 2:
                state = state[None, ...]
            if mem.ndim == 3:
                mem = mem[None, ...]
            if action_mask is None:
                mask = np.ones((state.shape[0], 4), dtype=np.float32)
            else:
                mask = np.asarray(action_mask, dtype=np.float32)
                if mask.ndim == 1:
                    mask = mask[None, ...]
            if not (
                np.isfinite(state).all()
                and np.isfinite(mem).all()
                and np.isfinite(mask).all()
            ):
                return PolicyActionV2("hold", seconds=0.2)
            if not np.isin(mask, (0.0, 1.0)).all() or mask.shape[-1] != 4:
                return PolicyActionV2("hold", seconds=0.2)
            outputs = self.session.run(
                None,
                {"state_history": state, "memory": mem, "action_mask": mask},
            )
            logits, continuous = np.asarray(outputs[0]), np.asarray(outputs[1])
            value, uncertainty = np.asarray(outputs[2]), np.asarray(outputs[3])
            if (
                logits.ndim != 2
                or continuous.ndim != 2
                or value.ndim != 2
                or uncertainty.ndim != 2
                or logits.shape[-1] != 4
                or continuous.shape[-1] != 5
                or value.shape[-1] != 1
                or uncertainty.shape[-1] != 1
                or not all(
                    np.isfinite(item).all() for item in (logits, continuous, value, uncertainty)
                )
            ):
                raise ValueError("invalid ONNX output shape")
            allowed = [bool(v) for v in mask[0].tolist()]
            index = int(np.argmax(logits[0]))
            if not any(allowed):
                index = 2
            elif not allowed[index]:
                index = 2 if allowed[2] else next(i for i, value in enumerate(allowed) if value)
            values = [float(v) for v in continuous[0]]
            if not all(math.isfinite(v) for v in values):
                raise ValueError("non-finite ONNX output")
            return PolicyActionV2(
                ACTION_KINDS[index],
                forward_m=values[0] if index == 0 else 0.0,
                right_m=values[1] if index == 0 else 0.0,
                up_m=values[2] if index == 0 else 0.0,
                yaw_delta_deg=values[3] if index == 1 else 0.0,
                seconds=values[4] if index == 2 else 0.0,
                action_mask=tuple(allowed) if any(allowed) else (True, True, True, True),
            )
        except Exception:
            return PolicyActionV2("hold", seconds=0.2)

    predict = act
    infer = act
    __call__ = act


__all__ = [
    "ACTION_KINDS",
    "HISTORY_LENGTH",
    "MEMORY_CHANNELS",
    "MEMORY_SIZE",
    "ONNXPolicyRunner",
    "PolicyONNXRunner",
    "PrimitivePolicy",
    "STATE_FEATURE_DIM",
    "export_onnx",
    "state_to_vector",
    "states_to_tensors",
]

# Descriptive alias retained for callers that prefer the artifact-oriented name.
PolicyONNXRunner = ONNXPolicyRunner
