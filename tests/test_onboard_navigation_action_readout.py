"""CPU small-tensor/readout fixtures, not real-model training or acceptance."""

from __future__ import annotations

import copy
import json
from math import pi

import pytest

torch = pytest.importorskip("torch")

from multidetect.autonomy.onboard_navigation_inference import parse_navigation_target  # noqa: E402
from scripts import onboard_navigation_action_readout as readout  # noqa: E402


@pytest.fixture(autouse=True)
def cpu_only(monkeypatch):
    assert not torch.cuda.is_initialized()

    def reject_cuda(*args, **kwargs):
        raise AssertionError("CPU component tests must never initialize CUDA")

    monkeypatch.setattr(torch.cuda, "_lazy_init", reject_cuda)
    yield
    assert not torch.cuda.is_initialized()


@pytest.fixture(scope="module", autouse=True)
def one_cpu_thread():
    previous = torch.get_num_threads()
    torch.set_num_threads(1)
    yield
    torch.set_num_threads(previous)


def provenance(**changes):
    fields = {name: "a" * 64 for name in readout.ActionReadoutProvenance.__dataclass_fields__}
    return readout.ActionReadoutProvenance(**(fields | changes))


def output(index=0, distance=1.75, angle=0.25, *, requires_grad=False):
    values = [-2.0, -2.0, -2.0, -2.0, distance, angle]
    values[index] = 2.0
    return torch.tensor([values], dtype=torch.float32, requires_grad=requires_grad)


@pytest.mark.parametrize("dtype", [torch.float32, torch.float16, torch.bfloat16])
@pytest.mark.parametrize("mask_dtype", [torch.int64, torch.bool])
def test_last_nonzero_handles_left_right_and_interior_padding(dtype, mask_dtype):
    hidden = torch.arange(5, dtype=dtype).reshape(1, 5, 1).expand(3, 5, 2048).clone()
    mask = torch.tensor([[1, 1, 0, 0, 0], [0, 0, 1, 1, 1], [0, 1, 0, 1, 0]], dtype=mask_dtype)
    hidden.requires_grad_()
    selected = readout.extract_last_prefix_feature(hidden, mask)
    assert selected.shape == (3, 2048)
    assert selected.dtype == torch.float32 and selected.device.type == "cpu"
    assert selected[:, 0].tolist() == [1, 4, 3]
    assert not selected.requires_grad
    hidden.detach().fill_(-100)
    assert selected[:, 0].tolist() == [1, 4, 3]


@pytest.mark.parametrize(
    "hidden,mask",
    [
        (torch.zeros(1, 3, 2047), torch.ones(1, 3, dtype=torch.int64)),
        (torch.zeros(1, 2048), torch.ones(1, 3, dtype=torch.int64)),
        (torch.zeros(0, 3, 2048), torch.ones(0, 3, dtype=torch.int64)),
        (torch.zeros(1, 0, 2048), torch.ones(1, 0, dtype=torch.int64)),
        (torch.zeros(1, 3, 2048, dtype=torch.int64), torch.ones(1, 3, dtype=torch.int64)),
        (torch.zeros(1, 3, 2048, dtype=torch.float64), torch.ones(1, 3, dtype=torch.int64)),
        (torch.zeros(1, 3, 2048), torch.ones(1, 3)),
        (torch.zeros(1, 3, 2048), torch.ones(3, dtype=torch.int64)),
        (torch.zeros(1, 3, 2048), torch.ones(2, 3, dtype=torch.int64)),
        (torch.zeros(1, 3, 2048), torch.tensor([[1, 2, 0]])),
        (torch.zeros(1, 3, 2048), torch.tensor([[1, -1, 0]])),
        (torch.zeros(1, 3, 2048), torch.zeros(1, 3, dtype=torch.int64)),
        (torch.zeros(2, 3, 2048), torch.tensor([[1, 1, 1], [0, 0, 0]])),
        (torch.full((1, 3, 2048), float("nan")), torch.ones(1, 3, dtype=torch.int64)),
        (torch.full((1, 3, 2048), float("inf")), torch.ones(1, 3, dtype=torch.int64)),
    ],
)
def test_feature_shape_dtype_binary_empty_and_nonfinite_rejected(hidden, mask):
    with pytest.raises(ValueError):
        readout.extract_last_prefix_feature(hidden, mask)


def test_sparse_or_non_tensor_features_rejected():
    with pytest.raises(ValueError):
        readout.extract_last_prefix_feature([], torch.ones(1, 1, dtype=torch.int64))
    with pytest.raises(ValueError):
        readout.extract_last_prefix_feature(
            torch.zeros(1, 1, 2048).to_sparse(), torch.ones(1, 1, dtype=torch.int64)
        )


def test_head_exact_small_fp32_sidecar_and_cpu_update_without_feature_gradients():
    head = readout.NavigationActionReadout()
    assert sum(parameter.numel() for parameter in head.parameters()) == 12294
    assert set(head.state_dict()) == {"linear.weight", "linear.bias"}
    assert all(
        parameter.dtype == torch.float32 and parameter.device.type == "cpu"
        for parameter in head.parameters()
    )
    features = torch.randn(4, 2048, requires_grad=True)
    labels = [
        readout.ActionReadoutLabel("forward", 2.25),
        readout.ActionReadoutLabel("turn_yaw_left", angle_magnitude_rad=0.4),
        readout.ActionReadoutLabel("turn_yaw_right", angle_magnitude_rad=0.2),
        readout.ActionReadoutLabel("sequence_stop"),
    ]
    optimizer = torch.optim.SGD(head.parameters(), lr=0.001)
    before = head.linear.weight.detach().clone()
    outputs = head(features)
    losses = readout.readout_loss(
        outputs, labels, class_weights=readout.class_balanced_weights(labels)
    )
    losses["total"].backward()
    assert features.grad is None
    assert all(
        parameter.grad is not None and torch.isfinite(parameter.grad).all()
        for parameter in head.parameters()
    )
    optimizer.step()
    assert not torch.equal(before, head.linear.weight)
    assert outputs.shape == (4, 6) and outputs.dtype == torch.float32


@pytest.mark.parametrize(
    "features",
    [
        torch.zeros(2048),
        torch.zeros(1, 2047),
        torch.zeros(0, 2048),
        torch.zeros(1, 2048).double(),
        torch.full((1, 2048), float("nan")),
    ],
)
def test_head_feature_validation(features):
    with pytest.raises(ValueError):
        readout.NavigationActionReadout()(features)


@pytest.mark.parametrize("mutation", ["half", "nan_weight", "inf_bias", "wrong_shape"])
def test_head_parameter_validation(mutation):
    head = readout.NavigationActionReadout()
    if mutation == "half":
        head.half()
    elif mutation == "wrong_shape":
        head.linear.weight = torch.nn.Parameter(torch.zeros(6, 20))
    else:
        with torch.no_grad():
            if mutation == "nan_weight":
                head.linear.weight[0, 0] = float("nan")
            else:
                head.linear.bias[0] = float("inf")
    with pytest.raises(ValueError):
        head(torch.zeros(1, 2048))


@pytest.mark.parametrize(
    "fields",
    [
        {"action_class": "up"},
        {"action_class": True},
        {"action_class": "sequence_stop", "distance_m": 1},
        {"action_class": "forward", "angle_magnitude_rad": 0.1},
        {"action_class": "turn_yaw_left", "distance_m": 1},
        *(
            {"action_class": "forward", "distance_m": value}
            for value in (True, 0, -1, 501, float("inf"), float("nan"))
        ),
        *(
            {"action_class": "turn_yaw_right", "angle_magnitude_rad": value}
            for value in (True, 0, -1, pi + 0.01, float("inf"), float("nan"))
        ),
    ],
)
def test_labels_reject_bool_nonfinite_range_and_wrong_metric_kind(fields):
    with pytest.raises(ValueError):
        readout.ActionReadoutLabel(**fields)


def test_class_weights_use_partition_counts_and_do_not_invent_absent_support():
    labels = [readout.ActionReadoutLabel("forward")] * 3 + [
        readout.ActionReadoutLabel("sequence_stop")
    ]
    weights = readout.class_balanced_weights(labels)
    assert weights.dtype == torch.float32 and weights.device.type == "cpu"
    assert weights.tolist() == pytest.approx([2 / 3, 0, 0, 2])
    # Three forward rows collectively have the same weight as one STOP row.
    assert weights[0].item() * 3 == pytest.approx(weights[3].item())


def test_partition_weights_do_not_cancel_on_singleton_or_same_class_microbatches():
    labels = [readout.ActionReadoutLabel("forward")] * 3 + [
        readout.ActionReadoutLabel("sequence_stop")
    ]
    weights = readout.class_balanced_weights(labels)
    for count in (1, 3):
        gradients, losses = [], []
        for multiplier in (1.0, 0.01):
            outputs = torch.zeros(count, 6, requires_grad=True)
            terms = readout.readout_loss(
                outputs, labels[:count], class_weights=weights * multiplier
            )
            terms["classification"].backward()
            losses.append(terms["classification"].item())
            gradients.append(outputs.grad[:, :4].clone())
        assert losses[1] == pytest.approx(losses[0] * 0.01)
        assert torch.allclose(gradients[1], gradients[0] * 0.01)
    full = torch.zeros(4, 6)
    full_loss = readout.readout_loss(full, labels, class_weights=weights)["classification"]
    singles = [
        readout.readout_loss(full[index : index + 1], [label], class_weights=weights)[
            "classification"
        ]
        for index, label in enumerate(labels)
    ]
    assert full_loss.item() == pytest.approx(torch.stack(singles).mean().item())
    assert full_loss.item() == pytest.approx(torch.log(torch.tensor(4.0)).item())


@pytest.mark.parametrize("labels", [[], "forward", [None], [{"action_class": "forward"}]])
def test_invalid_label_sequences_rejected(labels):
    with pytest.raises(ValueError):
        readout.class_balanced_weights(labels)


def test_loss_known_only_masks_denominators_units_and_gradients():
    labels = [
        readout.ActionReadoutLabel("forward", 2.0),
        readout.ActionReadoutLabel("forward"),
        readout.ActionReadoutLabel("turn_yaw_left", angle_magnitude_rad=0.5),
        readout.ActionReadoutLabel("sequence_stop"),
    ]
    outputs = torch.tensor(
        [
            [0, 0, 0, 0, 1, 200],
            [0, 0, 0, 0, 500, -200],
            [0, 0, 0, 0, -200, 0.25],
            [0, 0, 0, 0, 400, 400],
        ],
        dtype=torch.float32,
        requires_grad=True,
    )
    losses = readout.readout_loss(
        outputs, labels, class_weights=readout.class_balanced_weights(labels)
    )
    assert losses["distance"].item() == pytest.approx(0.5)
    assert losses["angle"].item() == pytest.approx(0.5 * 0.25**2)
    assert losses["classification"].item() == pytest.approx(torch.log(torch.tensor(4.0)).item())
    assert losses["total"].item() == pytest.approx(
        sum(losses[key].item() for key in ("classification", "distance", "angle"))
    )
    losses["total"].backward()
    assert outputs.grad[:, 4].tolist() == [-1, 0, 0, 0]
    assert outputs.grad[:, 5].tolist() == [0, 0, -0.25, 0]


@pytest.mark.parametrize(
    "label",
    [
        readout.ActionReadoutLabel("sequence_stop"),
        readout.ActionReadoutLabel("forward"),
        readout.ActionReadoutLabel("turn_yaw_left"),
    ],
)
def test_all_missing_metrics_zero_loss_and_zero_numeric_gradient(label):
    outputs = output(3, distance=-100, angle=100, requires_grad=True)
    losses = readout.readout_loss(
        outputs, [label], class_weights=readout.class_balanced_weights([label])
    )
    assert losses["distance"].item() == losses["angle"].item() == 0
    losses["total"].backward()
    assert outputs.grad[0, 4:].tolist() == [0, 0]


@pytest.mark.parametrize(
    "weights",
    [
        torch.ones(3),
        torch.ones(4).double(),
        torch.tensor([-1.0, 1, 1, 1]),
        torch.zeros(4),
        torch.tensor([0.0, 1, 1, 1]),
        torch.tensor([float("nan"), 1, 1, 1]),
    ],
)
def test_loss_weight_gate(weights):
    with pytest.raises(ValueError):
        readout.readout_loss(
            output(), [readout.ActionReadoutLabel("forward")], class_weights=weights
        )


@pytest.mark.parametrize(
    "outputs",
    [
        torch.zeros(6),
        torch.zeros(1, 5),
        torch.zeros(0, 6),
        torch.zeros(1, 6).double(),
        torch.full((1, 6), float("nan")),
        torch.full((1, 6), float("inf")),
    ],
)
def test_output_shape_dtype_and_nonfinite_gate(outputs):
    with pytest.raises(ValueError):
        readout.readout_result(outputs, provenance=provenance())
    with pytest.raises(ValueError):
        readout.readout_loss(
            outputs, [readout.ActionReadoutLabel("forward")], class_weights=torch.ones(4)
        )


def test_loss_misaligned_rows_and_finite_input_loss_overflow_refused():
    with pytest.raises(ValueError):
        readout.readout_loss(output(), [], class_weights=torch.ones(4))
    with pytest.raises(ValueError):
        readout.readout_loss(
            output(), [readout.ActionReadoutLabel("forward")] * 2, class_weights=torch.ones(4)
        )
    huge = torch.tensor([[-3e38, 3e38, 0, 0, 0, 0]], dtype=torch.float32)
    with pytest.raises(ValueError, match="overflowed"):
        readout.readout_loss(
            huge, [readout.ActionReadoutLabel("forward")], class_weights=torch.ones(4)
        )


@pytest.mark.parametrize(
    "index,kind,translation,yaw",
    [
        (0, "translate", [1.75, 0, 0], None),
        (1, "turn_yaw", None, -0.25),
        (2, "turn_yaw", None, 0.25),
        (3, "stop", None, None),
    ],
)
def test_separate_raw_provenance_and_exact_typed_frd_yaw_null_mapping(
    index, kind, translation, yaw
):
    row = output(index)
    before = row.clone()
    result = readout.readout_result(row, provenance=provenance())
    target = parse_navigation_target(json.dumps(result["target"], allow_nan=False))
    assert target.kind == kind
    assert result["target"]["translation_m"] == translation
    assert result["target"]["yaw_delta_rad"] == yaw
    assert result["raw_logits"] == row[0, :4].tolist()
    assert result["raw_distance_m"] == 1.75 and result["raw_angle_magnitude_rad"] == 0.25
    assert result["selected_class"] == readout.CLASS_NAMES[index]
    assert result["source_kind"] == "learned_action_readout_not_text_generation"
    assert all(value == "a" * 64 for value in result["declared_bindings"].values())
    assert all(
        result[name] is False
        for name in (
            "actual_model_identity_verified",
            "prefix_only_verified",
            "quality_claim",
            "candidate_promoted",
            "execution_authority",
            "final_release_allowed",
        )
    )
    assert not {"generated", "raw", "token_ids", "generated_token_ids", "elapsed_s"} & result.keys()
    assert torch.equal(before, row)


@pytest.mark.parametrize(
    "index,distance,angle",
    [(0, 0, 0.2), (0, -1, 0.2), (0, 500.01, 0.2), (1, 2, 0), (1, 2, -0.2), (2, 2, pi + 0.01)],
)
def test_selected_scale_rejected_without_clip_abs_or_fixed_default(index, distance, angle):
    row = output(index, distance, angle)
    with pytest.raises(readout.ActionReadoutRejected) as caught:
        readout.readout_result(row, provenance=provenance())
    raw = caught.value.observation
    assert raw["raw_distance_m"] == pytest.approx(distance)
    assert raw["raw_angle_magnitude_rad"] == pytest.approx(angle)
    assert "target" not in raw


def test_unused_scales_are_preserved_not_supervised_or_inserted_in_target():
    result = readout.readout_result(output(3, -10, -20), provenance=provenance())
    assert result["raw_distance_m"] == -10 and result["raw_angle_magnitude_rad"] == -20
    assert result["target"]["translation_m"] is result["target"]["yaw_delta_rad"] is None
    result = readout.readout_result(output(2, -10, 0.17), provenance=provenance())
    assert result["target"]["translation_m"] is None
    assert result["target"]["yaw_delta_rad"] == pytest.approx(0.17)
    assert result["raw_distance_m"] == -10


@pytest.mark.parametrize("value", ["A" * 64, "a" * 63, "g" * 64, None, True])
def test_provenance_rejects_bad_declared_sha(value):
    with pytest.raises((TypeError, ValueError)):
        provenance(prefix_sha256=value)


def test_result_requires_one_row_exact_provenance_and_state_dict_roundtrip_cpu_only():
    with pytest.raises(ValueError):
        readout.readout_result(output().expand(2, 6), provenance=provenance())
    with pytest.raises(ValueError):
        readout.readout_result(output(), provenance={})
    head = readout.NavigationActionReadout()
    copied = readout.NavigationActionReadout()
    copied.load_state_dict(copy.deepcopy(head.state_dict()), strict=True)
    features = torch.randn(2, 2048)
    assert torch.equal(head(features), copied(features))
    assert set(head.state_dict()) == {"linear.weight", "linear.bias"}
