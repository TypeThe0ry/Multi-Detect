"""Focused model boundary tests; skipped when optional ML dependencies are absent."""

from __future__ import annotations

import numpy as np
import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("onnx")
pytest.importorskip("onnxruntime")

from multidetect.autonomy.policy_v2.model import (  # noqa: E402
    PrimitivePolicy,
    export_onnx,
)


def _inputs(batch: int = 2):
    torch.manual_seed(4)
    return (
        torch.randn(batch, 16, 128),
        torch.randn(batch, 5, 16, 16),
        torch.ones(batch, 4),
    )


def test_forward_shapes_and_masked_logits() -> None:
    model = PrimitivePolicy(hidden_size=32).eval()
    state, memory, mask = _inputs()
    mask[0] = torch.tensor([1.0, 0.0, 0.0, 0.0])
    logits, continuous, value, uncertainty = model(state, memory, mask)
    assert logits.shape == (2, 4)
    assert continuous.shape == (2, 5)
    assert value.shape == (2, 1)
    assert uncertainty.shape == (2, 1)
    assert torch.all(logits[0, 1:] < -1e8)


@pytest.mark.parametrize(
    "allowed",
    [
        [True, False, False, False],
        [False, True, False, False],
        [False, False, True, False],
        [False, False, False, True],
    ],
)
def test_act_respects_mask_and_policy_bounds(allowed: list[bool]) -> None:
    model = PrimitivePolicy(hidden_size=32).eval()
    state, memory, _ = _inputs(batch=1)
    action = model.act(state[0], memory[0], allowed)
    assert action.kind == ("translate", "turn", "hold", "stop")[allowed.index(True)]
    assert -2.0 <= action.forward_m <= 8.0
    assert -6.0 <= action.right_m <= 6.0
    assert -2.0 <= action.up_m <= 2.0
    assert -90.0 <= action.yaw_delta_deg <= 90.0
    assert action.kind != "hold" or 0.2 <= action.seconds <= 2.0


def test_nan_input_fails_closed_to_hold() -> None:
    model = PrimitivePolicy(hidden_size=32).eval()
    state, memory, _ = _inputs(batch=1)
    state[0, 0, 0] = float("nan")
    action = model.act(state[0], memory[0])
    assert action.kind == "hold"
    assert action.seconds == pytest.approx(0.2)


def test_onnx_export_ort_parity(tmp_path) -> None:
    import onnxruntime as ort

    model = PrimitivePolicy(hidden_size=32).eval()
    state, memory, mask = _inputs(batch=1)
    with torch.no_grad():
        expected = model(state, memory, mask)
    path = export_onnx(model, tmp_path / "policy.onnx")
    session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    actual = session.run(
        None,
        {
            "state_history": state.numpy(),
            "memory": memory.numpy(),
            "action_mask": mask.numpy(),
        },
    )
    for expected_tensor, actual_array in zip(expected, actual, strict=True):
        np.testing.assert_allclose(expected_tensor.numpy(), actual_array, rtol=2e-3, atol=3e-4)
