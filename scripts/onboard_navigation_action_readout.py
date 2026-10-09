"""Experimental small action readout, not a model loader or flight admission.

The caller owns exact frozen base/LoRA admission and truth-free prefix-only
extraction. Supplied hidden states and SHA declarations do not prove either.
The intended probe groups complete episodes (LOEO), never random adjacent
frames; backbone exposure to all86 is not an independent holdout. Lock the
head/checkpoint rule before inspecting17+42 development rows. No actual probe,
optimizer campaign, feature extraction, or real-model acceptance runs here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import isfinite, pi

import torch
from torch import nn
from torch.nn import functional as F

from multidetect.autonomy.onboard_navigation_inference import PlannedNavigationTarget
from multidetect.autonomy.serialization import require_sha256

CONTRACT = "multidetect.onboard-navigation-action-readout-v1"
FEATURE_DIM = 2048
CLASS_NAMES = ("forward", "turn_yaw_left", "turn_yaw_right", "sequence_stop")


def _tensor(value, name, *, shape, dtypes):
    if not isinstance(value, torch.Tensor) or value.layout != torch.strided:
        raise ValueError(f"{name} must be a dense tensor")
    if value.dtype not in dtypes or value.ndim != len(shape):
        raise ValueError(f"{name} has unsupported dtype/rank")
    if any(
        actual < 1 or wanted is not None and actual != wanted
        for actual, wanted in zip(value.shape, shape, strict=True)
    ):
        raise ValueError(f"{name} has unsupported shape")
    if not torch.isfinite(value).all().item():
        raise ValueError(f"{name} must be finite")
    return value


def extract_last_prefix_feature(hidden_states, attention_mask):
    """Snapshot the last nonzero mask position; cannot authenticate prefix-only.

    Masks are binary bool/int64, including left padding or interior masked
    positions. The returned detached FP32 tensor cannot backpropagate into a
    caller's backbone; source dtype may be FP32/BF16/FP16. No model is called.
    """
    hidden = _tensor(
        hidden_states,
        "hidden_states",
        shape=(None, None, FEATURE_DIM),
        dtypes=(torch.float32, torch.bfloat16, torch.float16),
    )
    mask = _tensor(
        attention_mask,
        "attention_mask",
        shape=tuple(hidden.shape[:2]),
        dtypes=(torch.bool, torch.int64),
    )
    if mask.device != hidden.device or not ((mask == 0) | (mask == 1)).all().item():
        raise ValueError("mask must be binary and on the hidden-state device")
    valid = mask != 0
    if not valid.any(dim=1).all().item():
        raise ValueError("every prefix requires an unmasked token")
    positions = torch.arange(hidden.shape[1], device=hidden.device).expand_as(mask)
    last = positions.masked_fill(~valid, -1).max(dim=1).values
    rows = torch.arange(hidden.shape[0], device=hidden.device)
    return hidden[rows, last].detach().to(dtype=torch.float32).clone()


class NavigationActionReadout(nn.Module):
    """Only a FP32 linear sidecar; deliberately independent of base/LoRA state."""

    def __init__(self):
        super().__init__()
        self.linear = nn.Linear(FEATURE_DIM, 6, device="cpu", dtype=torch.float32)

    def forward(self, features):
        features = _tensor(features, "features", shape=(None, FEATURE_DIM), dtypes=(torch.float32,))
        weight = _tensor(
            self.linear.weight, "head weight", shape=(6, FEATURE_DIM), dtypes=(torch.float32,)
        )
        bias = _tensor(self.linear.bias, "head bias", shape=(6,), dtypes=(torch.float32,))
        if features.device != weight.device or bias.device != weight.device:
            raise ValueError("features and FP32 head must share a device")
        output = self.linear(features.detach())
        return _outputs(output)


def _positive(value, name, maximum):
    if type(value) not in (int, float) or not isfinite(value) or not 0 < value <= maximum:
        raise ValueError(f"{name} must be a finite positive bounded magnitude")
    return float(value)


@dataclass(frozen=True, slots=True)
class ActionReadoutLabel:
    """Explicit class plus optional known metric; None is not a zero target."""

    action_class: str
    distance_m: float | None = None
    angle_magnitude_rad: float | None = None

    def __post_init__(self):
        if type(self.action_class) is not str or self.action_class not in CLASS_NAMES:
            raise ValueError("unsupported readout label class")
        if self.distance_m is not None:
            if self.action_class != "forward":
                raise ValueError("only forward has a distance label")
            object.__setattr__(self, "distance_m", _positive(self.distance_m, "distance", 500))
        if self.angle_magnitude_rad is not None:
            if self.action_class not in CLASS_NAMES[1:3]:
                raise ValueError("only yaw classes have an angle label")
            object.__setattr__(
                self, "angle_magnitude_rad", _positive(self.angle_magnitude_rad, "angle", pi)
            )


def _labels(labels, count=None):
    if (
        not isinstance(labels, Sequence)
        or not labels
        or any(type(label) is not ActionReadoutLabel for label in labels)
        or count is not None
        and len(labels) != count
    ):
        raise ValueError("nonempty exact readout labels must align with output rows")
    return labels


def class_balanced_weights(labels):
    """Compute CPU FP32 inverse counts once from the admitted training partition.

    A class absent from that partition has weight zero, not invented support.
    Pass these weights to every loss call; do not also oversample by class.
    """
    labels = _labels(labels)
    counts = [sum(label.action_class == name for label in labels) for name in CLASS_NAMES]
    present = sum(count > 0 for count in counts)
    return torch.tensor(
        [len(labels) / (present * count) if count else 0 for count in counts],
        dtype=torch.float32,
        device="cpu",
    )


def _outputs(value):
    return _tensor(value, "readout outputs", shape=(None, 6), dtypes=(torch.float32,))


def readout_loss(outputs, labels, *, class_weights):
    """Class-balanced CE plus known-only SmoothL1 in metres/radians (beta=1).

    Each numeric mean uses only its known labels; all-missing/STOP batches
    yield differentiable zero numeric losses without supervising a zero scale.
    No clipping/absolute value transforms the predicted magnitudes.
    """
    outputs = _outputs(outputs)
    labels = _labels(labels, outputs.shape[0])
    weights = _tensor(class_weights, "class_weights", shape=(4,), dtypes=(torch.float32,))
    if weights.device != outputs.device or (weights < 0).any().item():
        raise ValueError("class weights must be nonnegative and share the output device")
    classes = torch.tensor(
        [CLASS_NAMES.index(label.action_class) for label in labels],
        dtype=torch.int64,
        device=outputs.device,
    )
    if not (weights[classes] > 0).all().item():
        raise ValueError("every observed label requires a positive training-partition weight")
    # Keep the partition weights effective for singleton/same-class batches.
    classification = F.cross_entropy(
        outputs[:, :4], classes, weight=weights, reduction="none"
    ).mean()
    terms = []
    for column, name in ((4, "distance_m"), (5, "angle_magnitude_rad")):
        known = [
            (index, getattr(label, name))
            for index, label in enumerate(labels)
            if getattr(label, name) is not None
        ]
        if known:
            indices, values = zip(*known, strict=True)
            expected = torch.tensor(values, dtype=torch.float32, device=outputs.device)
            terms.append(F.smooth_l1_loss(outputs[list(indices), column], expected, beta=1.0))
        else:
            terms.append((outputs[:, column] * 0.0).sum())
    losses = {
        "total": classification + terms[0] + terms[1],
        "classification": classification,
        "distance": terms[0],
        "angle": terms[1],
    }
    if any(not torch.isfinite(loss).item() for loss in losses.values()):
        raise ValueError("readout loss overflowed or is nonfinite")
    return losses


@dataclass(frozen=True, slots=True)
class ActionReadoutProvenance:
    """Caller-declared SHA bindings only, not authentication of loaded parameters."""

    prefix_sha256: str
    base_weights_sha256: str
    adapter_weights_sha256: str
    head_weights_sha256: str
    head_config_sha256: str

    def __post_init__(self):
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if require_sha256(value, name) != value:
                raise ValueError("readout provenance requires lowercase canonical SHA-256")


class ActionReadoutRejected(ValueError):
    """Selected scale rejected; raw finite outputs remain available separately."""

    def __init__(self, message, observation):
        self.observation = observation
        super().__init__(message)


def readout_result(outputs, *, provenance):
    """One standalone observation/target, never model text, token IDs or flight.

    All raw outputs must be finite. Only the selected action's scale is used
    and range-checked; unused finite scales survive verbatim in the observation.
    STOP means a sequence primitive, not landing or mission completion.
    The caller separately persists/binds this record and any sidecar weights.
    """
    outputs = _outputs(outputs)
    if outputs.shape[0] != 1 or type(provenance) is not ActionReadoutProvenance:
        raise ValueError("one output row and exact provenance are required")
    values = outputs[0].detach().tolist()
    index = max(range(4), key=lambda column: values[column])
    observation = {
        "schema_version": 1,
        "contract": CONTRACT,
        "source_kind": "learned_action_readout_not_text_generation",
        "class_names": list(CLASS_NAMES),
        "selected_class": CLASS_NAMES[index],
        "raw_logits": values[:4],
        "raw_distance_m": values[4],
        "raw_angle_magnitude_rad": values[5],
        "declared_bindings": {
            name: getattr(provenance, name) for name in provenance.__dataclass_fields__
        },
        "actual_model_identity_verified": False,
        "prefix_only_verified": False,
        "quality_claim": False,
        "candidate_promoted": False,
        "execution_authority": False,
        "final_release_allowed": False,
    }
    try:
        if index == 0:
            target = PlannedNavigationTarget(
                "translate", (_positive(values[4], "distance", 500), 0.0, 0.0)
            )
        elif index in (1, 2):
            angle = _positive(values[5], "angle", pi)
            target = PlannedNavigationTarget(
                "turn_yaw", yaw_delta_rad=(-angle if index == 1 else angle)
            )
        else:
            target = PlannedNavigationTarget("stop")
    except ValueError as exc:
        raise ActionReadoutRejected(str(exc), observation) from exc
    return observation | {"target": target.to_mapping()}
