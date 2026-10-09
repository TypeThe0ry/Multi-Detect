from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from test_onboard_sequence_windows import _document
from test_train_onboard_model import _write_trajectory_fixture

from multidetect.autonomy.contracts import ContextSnapshot
from multidetect.autonomy.onboard_agent import OnboardModelInput
from scripts import onboard_sequence_windows as sequences
from scripts import train_onboard_history_model as history
from scripts import train_onboard_model as trainer


def _episodes(tmp_path: Path, *, length: int = 5):
    document = _document()
    episode = document["episodes"][0]
    template = episode["transitions"][0]
    transitions = []
    for index in range(length):
        step = copy.deepcopy(template)
        timestamp = float(episode["seed"]) + index * 0.25
        step.update(
            sequence=index,
            transition_id=f"training-transition-{index}",
            frame_id=f"training-frame-{index}",
            next_frame_id=f"training-frame-{index + 1}" if index + 1 < length else None,
            observed_at_s=timestamp,
        )
        context_raw = step["observation"]["context"]
        context_raw.update(
            snapshot_id=f"training-context-{index}",
            captured_at_s=timestamp,
            battery_remaining_pct=80 - index,
        )
        event_raw = step["observation"]["event"]
        event_raw.update(event_id=f"training-event-{index}", observed_at_s=timestamp)
        context = ContextSnapshot.from_mapping(context_raw)
        event = trainer._onboard_event_from_mapping(event_raw)
        step["features"] = list(OnboardModelInput(context, event).feature_vector())
        step["source"].update(
            frame_id=step["frame_id"],
            captured_at_s=timestamp,
            context_sha256=context.canonical_hash,
            event_sha256=event.canonical_hash,
            rgb_payload_sha256=hashlib.sha256(f"history-{index}:rgb".encode()).hexdigest(),
            depth_payload_sha256=hashlib.sha256(f"history-{index}:depth".encode()).hexdigest(),
        )
        transitions.append(step)
    episode["transitions"] = transitions
    path = _write_trajectory_fixture(tmp_path, document=document)
    return sequences.load_trajectory_episodes(path, allow_unreviewed=True), path


@pytest.fixture
def parent_onnx(tmp_path: Path):
    torch = pytest.importorskip("torch")
    pytest.importorskip("onnx")
    pytest.importorskip("onnxruntime")
    torch.manual_seed(33)
    model = history.build_parent_model().eval()
    path = tmp_path / "fixture-parent.onnx"
    torch.onnx.export(
        model,
        (torch.zeros((1, 40)),),
        path,
        input_names=["features"],
        output_names=["policy_output"],
        opset_version=17,
        dynamo=False,
    )
    return model, path, trainer._sha256(path)


def _inputs(count: int = 8):
    torch = pytest.importorskip("torch")
    features = torch.arange(320, dtype=torch.float32).reshape(1, 8, 40) / 320
    valid = torch.tensor([[0.0] * (8 - count) + [1.0] * count])
    dt = torch.tensor([[0.0] * (8 - count) + [0.0] + [0.25] * (count - 1)])
    reset = torch.zeros((1, 8))
    reset[0, 8 - count] = 1.0
    return features, valid, dt, reset


def test_import_does_not_load_heavy_training_or_export_libraries() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from scripts import train_onboard_history_model; "
            "assert not ({'torch', 'onnx', 'onnxruntime'} & set(sys.modules))",
        ],
        cwd=Path(history.__file__).resolve().parents[1],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_exact_parent_shape_and_parameter_count() -> None:
    pytest.importorskip("torch")
    parent = history.build_parent_model()
    assert len(parent.state_dict()) == 10
    assert sum(parameter.numel() for parameter in parent.parameters()) == 15188
    assert tuple(parent.state_dict()["body.0.weight"].shape) == (96, 40)


def test_unique_causal_prefix_windows_are_right_aligned(tmp_path: Path) -> None:
    episodes, _ = _episodes(tmp_path)
    windows = history.build_history_windows(episodes, require_reviewed=False)
    assert len(windows) == sum(len(episode.steps) for episode in episodes)
    train = windows[:5]
    assert [window.current.sequence for window in train] == list(range(5))
    for index, window in enumerate(train):
        assert [step.sequence for step in window.steps if step is not None] == list(
            range(index + 1)
        )
        assert window.valid_mask == (0.0,) * (7 - index) + (1.0,) * (index + 1)
        assert window.features[: 7 - index] == ((0.0,) * 40,) * (7 - index)
        assert window.dt_s[: 7 - index] == (0.0,) * (7 - index)
        assert window.reset_mask == (0.0,) * (7 - index) + (1.0,) + (0.0,) * index
    assert all(window.current.identity == window.episode.identity for window in windows)


def test_truncated_prefix_does_not_forge_episode_reset_or_drop_predecessor_dt(
    tmp_path: Path,
) -> None:
    episodes, _ = _episodes(tmp_path, length=12)
    windows = history.build_history_windows(episodes, require_reviewed=False)
    ninth = windows[8]
    assert [step.sequence for step in ninth.steps] == list(range(1, 9))
    assert ninth.reset_mask == (0.0,) * 8
    assert ninth.dt_s == (0.25,) * 8
    assert [step.sequence for step in windows[11].steps] == list(range(4, 12))


def test_changing_future_observations_cannot_change_existing_prefixes(tmp_path: Path) -> None:
    episodes, _ = _episodes(tmp_path, length=12)
    before = history.build_history_windows(episodes, require_reviewed=False)
    future = episodes[0].steps[-1]
    context = replace(future.context, battery_remaining_pct=19.0)
    sample = replace(
        future.sample, features=OnboardModelInput(context, future.event).feature_vector()
    )
    changed = replace(future, context=context, sample=sample)
    changed_episode = replace(episodes[0], steps=(*episodes[0].steps[:-1], changed))
    after = history.build_history_windows((changed_episode, *episodes[1:]), require_reviewed=False)
    assert [window.features for window in before[:11]] == [window.features for window in after[:11]]
    assert before[11].features != after[11].features


def test_collator_uses_only_current_label(tmp_path: Path) -> None:
    torch = pytest.importorskip("torch")
    episodes, _ = _episodes(tmp_path)
    windows = history.build_history_windows(episodes, require_reviewed=False)
    tensors = history.windows_to_tensors(windows)
    assert tensors["features"].shape == (8, 8, 40)
    assert tensors["features"].dtype == torch.float32
    assert tensors["task_labels"].shape == (8,)
    assert tensors["regression_labels"].shape == (8, 4)
    assert tensors["task_labels"].tolist() == [
        window.current.sample.task_label for window in windows
    ]


def test_default_collation_and_training_reject_unreviewed_episodes(tmp_path: Path) -> None:
    episodes, _ = _episodes(tmp_path)
    with pytest.raises(ValueError, match="independently reviewed"):
        history.build_history_windows(episodes)
    pytest.importorskip("torch")
    windows = history.build_history_windows(episodes, require_reviewed=False)
    with pytest.raises(ValueError, match="independently reviewed"):
        history.train_history_model(history.build_history_model(), windows, epochs=1, seed=1)


def test_formal_loader_uses_strict_existing_admission(tmp_path: Path, monkeypatch) -> None:
    episodes, path = _episodes(tmp_path)
    calls = []

    def load(*args, **kwargs):
        calls.append((args, kwargs))
        return tuple(replace(episode, training_ready=True) for episode in episodes)

    monkeypatch.setattr(sequences, "load_trajectory_episodes", load)
    windows = history.load_training_windows(path)
    assert len(windows) == 8
    assert calls == [((path,), {})]


def test_formal_loader_requires_all_independent_declared_splits(
    tmp_path: Path, monkeypatch
) -> None:
    episodes, path = _episodes(tmp_path)
    monkeypatch.setattr(
        sequences,
        "load_trajectory_episodes",
        lambda _: tuple(replace(episode, training_ready=True) for episode in episodes[:2]),
    )
    with pytest.raises(ValueError, match="independent train/evaluation/test"):
        history.load_training_windows(path)


def test_source_identity_cannot_cross_splits(tmp_path: Path) -> None:
    episodes, _ = _episodes(tmp_path)
    duplicate = replace(episodes[0], identity=replace(episodes[0].identity, split="test"))
    with pytest.raises(ValueError):
        history.build_history_windows((*episodes, duplicate), require_reviewed=False)


def test_history_windows_reject_home_change_instead_of_silently_splitting(tmp_path: Path) -> None:
    episodes, _ = _episodes(tmp_path)
    step = episodes[0].steps[1]
    home = replace(
        step.context.home_position, latitude_deg=step.context.home_position.latitude_deg + 0.01
    )
    context = replace(step.context, home_position=home)
    sample = replace(step.sample, features=OnboardModelInput(context, step.event).feature_vector())
    changed = replace(step, context=context, sample=sample)
    episode = replace(episodes[0], steps=(episodes[0].steps[0], changed, *episodes[0].steps[2:]))
    with pytest.raises(ValueError, match="home identity changed; a separate episode"):
        history.build_history_windows((episode, *episodes[1:]), require_reviewed=False)


def test_history_windows_reject_gap_that_runtime_would_reset(tmp_path: Path) -> None:
    episodes, _ = _episodes(tmp_path)
    adjusted_steps = []
    for index, step in enumerate(episodes[0].steps):
        timestamp = step.observed_at_s + (6.0 if index >= 1 else 0.0)
        context = replace(step.context, captured_at_s=timestamp)
        event = replace(step.event, observed_at_s=timestamp)
        sample = replace(step.sample, features=OnboardModelInput(context, event).feature_vector())
        adjusted_steps.append(
            replace(
                step,
                context=context,
                event=event,
                sample=sample,
                observed_at_s=timestamp,
                dt_s=6.25 if index == 1 else step.dt_s,
            )
        )
    episode = replace(episodes[0], steps=tuple(adjusted_steps))
    with pytest.raises(ValueError, match="gap exceeds 5 seconds"):
        history.build_history_windows((episode, *episodes[1:]), require_reviewed=False)


def test_migration_calls_existing_strict_loader_and_copies_all_parent_tensors(
    parent_onnx, monkeypatch
) -> None:
    torch = pytest.importorskip("torch")
    parent, path, digest = parent_onnx
    original = trainer.initialize_from_parent
    calls = []

    def initialize(model, parent_path, expected_sha256):
        assert set(model.state_dict()) == set(parent.state_dict())
        calls.append((parent_path, expected_sha256))
        return original(model, parent_path, expected_sha256)

    monkeypatch.setattr(trainer, "initialize_from_parent", initialize)
    model = history.build_history_model()
    receipt = history.initialize_history_from_parent(model, path, digest)
    assert calls == [(path.resolve(), digest)]
    assert receipt["inherited_parameters"] == 15188
    assert receipt["parent_initializer_count"] == 10
    assert receipt["migration_strict"] is True
    for name, tensor in parent.state_dict().items():
        assert torch.equal(model.state_dict()[name], tensor)
    assert torch.count_nonzero(model.temporal_projection.weight) == 0
    assert torch.count_nonzero(model.temporal_projection.bias) == 0


@pytest.mark.parametrize("count", [1, 3, 8])
def test_zero_residual_matches_parent_raw_onnx_for_current_observation(
    parent_onnx, count: int
) -> None:
    np = pytest.importorskip("numpy")
    ort = pytest.importorskip("onnxruntime")
    torch = pytest.importorskip("torch")
    parent, path, digest = parent_onnx
    model = history.build_history_model()
    receipt = history.initialize_history_from_parent(model, path, digest)
    inputs = _inputs(count)
    with torch.no_grad():
        actual = model(*inputs).numpy()
        torch_parent = parent(inputs[0][:, -1, :]).numpy()
    raw = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"]).run(
        None, {"features": inputs[0][:, -1, :].numpy()}
    )[0]
    assert np.allclose(actual, raw, atol=1e-5)
    assert np.allclose(actual, torch_parent, rtol=1e-5, atol=1e-6)
    assert receipt["parent_raw_onnx_parity_passed"] is True


def test_migration_rejects_hash_mismatch_before_weight_copy(parent_onnx) -> None:
    _, path, _ = parent_onnx
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        history.initialize_history_from_parent(history.build_history_model(), path, "0" * 64)


def test_migration_rejects_extra_initializer_even_if_hash_matches(parent_onnx) -> None:
    np = pytest.importorskip("numpy")
    onnx = pytest.importorskip("onnx")
    _, path, _ = parent_onnx
    document = onnx.load(path)
    document.graph.initializer.append(
        onnx.numpy_helper.from_array(np.ones(1, dtype=np.float32), "extra")
    )
    onnx.save(document, path)
    with pytest.raises(ValueError, match="exactly 10"):
        history.initialize_history_from_parent(
            history.build_history_model(), path, trainer._sha256(path)
        )


def test_migration_does_not_silently_expand_legacy_feature_width(parent_onnx) -> None:
    onnx = pytest.importorskip("onnx")
    _, path, _ = parent_onnx
    document = onnx.load(path)
    for index, tensor in enumerate(document.graph.initializer):
        if tensor.name == "body.0.weight":
            values = onnx.numpy_helper.to_array(tensor)[:, :36]
            document.graph.initializer[index].CopyFrom(
                onnx.numpy_helper.from_array(values, tensor.name)
            )
            break
    onnx.save(document, path)
    with pytest.raises(ValueError, match="40-feature parent"):
        history.initialize_history_from_parent(
            history.build_history_model(), path, trainer._sha256(path)
        )


def test_migration_rejects_changed_graph_semantics_with_same_parent_weights(parent_onnx) -> None:
    onnx = pytest.importorskip("onnx")
    _, path, _ = parent_onnx
    document = onnx.load(path)
    for node in document.graph.node:
        if node.op_type == "Relu":
            node.op_type = "Sigmoid"
            break
    onnx.save(document, path)
    with pytest.raises(ValueError, match="raw ONNX outputs"):
        history.initialize_history_from_parent(
            history.build_history_model(), path, trainer._sha256(path)
        )


def test_padding_features_hidden_dt_and_reset_do_not_pollute_learned_residual() -> None:
    torch = pytest.importorskip("torch")
    model = history.build_history_model()
    with torch.no_grad():
        model.temporal_projection.weight.fill_(0.1)
    inputs = _inputs(3)
    changed = tuple(item.clone() for item in inputs)
    changed[0][:, :5] = float("nan")
    changed[2][:, :5] = float("nan")
    changed[3][:, :5] = float("nan")
    with torch.no_grad():
        reference = model(*inputs)
        actual = model(*changed)
    assert torch.equal(reference, actual)
    assert torch.isfinite(actual).all()


def test_history_residual_is_order_sensitive_and_covers_the_oldest_slot() -> None:
    torch = pytest.importorskip("torch")
    model = history.build_history_model()
    with torch.no_grad():
        model.body[0].weight.zero_()
        model.body[0].bias.zero_()
        model.body[0].weight[0, 0] = 1
        model.body[2].weight.zero_()
        model.body[2].bias.zero_()
        model.body[2].weight[0, 0] = 1
        model.temporal_conv.weight.zero_()
        model.temporal_conv.bias.zero_()
        model.temporal_conv.weight[0, 0, 0] = 1
        model.temporal_conv.weight[0, 0, 1] = 2
        model.temporal_projection.weight.zero_()
        model.temporal_projection.weight[0, 0] = 1
        model.task.weight.zero_()
        model.task.bias.zero_()
        model.task.weight[0, 0] = 1
    inputs = _inputs()
    inputs[0].zero_()
    inputs[0][0, 0, 0], inputs[0][0, 1, 0] = 1, 3
    swapped = tuple(item.clone() for item in inputs)
    swapped[0][0, 0, 0], swapped[0][0, 1, 0] = 3, 1
    assert model(*inputs)[0, 0].item() == 7
    assert model(*swapped)[0, 0].item() == 5
    oldest_changed = tuple(item.clone() for item in inputs)
    oldest_changed[0][0, 0, 0] = 4
    assert model(*oldest_changed)[0, 0].item() == 10


def test_zero_projection_can_learn_then_conv_and_shared_body_receive_gradients() -> None:
    torch = pytest.importorskip("torch")
    torch.manual_seed(5)
    model = history.build_history_model()
    loss = model(*_inputs()).square().sum()
    loss.backward()
    assert torch.count_nonzero(model.temporal_projection.weight.grad) > 0
    assert torch.count_nonzero(model.body[0].weight.grad) > 0
    assert torch.count_nonzero(model.temporal_conv.weight.grad) == 0
    with torch.no_grad():
        model.temporal_projection.weight.add_(-0.01 * model.temporal_projection.weight.grad)
    model.zero_grad(set_to_none=True)
    model(*_inputs()).square().sum().backward()
    assert torch.count_nonzero(model.temporal_conv.weight.grad) > 0


def test_dt_seconds_are_normalized_and_clipped_inside_model() -> None:
    torch = pytest.importorskip("torch")
    model = history.build_history_model()
    captured = []
    handle = model.temporal_conv.register_forward_pre_hook(
        lambda _, args: captured.append(args[0].detach())
    )
    features, valid, dt, reset = _inputs(3)
    dt[0, 5:] = torch.tensor([0.25, 15.0, 60.0])
    model(features, valid, dt, reset)
    handle.remove()
    assert captured[0][0, 97, :5].tolist() == [0.0] * 5
    assert captured[0][0, 97, 5:].tolist() == pytest.approx([0.25 / 30, 0.5, 1.0])


def test_onnx_export_four_inputs_fixed_shapes_and_metadata(parent_onnx, tmp_path: Path) -> None:
    onnx = pytest.importorskip("onnx")
    _, parent_path, digest = parent_onnx
    model = history.build_history_model()
    history.initialize_history_from_parent(model, parent_path, digest)
    output = tmp_path / "fixture-history.onnx"
    receipt = history.export_history_onnx(model, output, parent_sha256=digest)
    document = onnx.load(output)
    assert [item.name for item in document.graph.input] == list(history.INPUT_NAMES)
    assert [
        [d.dim_value for d in item.type.tensor_type.shape.dim] for item in document.graph.input
    ] == [[1, 8, 40], [1, 8], [1, 8], [1, 8]]
    assert [d.dim_value for d in document.graph.output[0].type.tensor_type.shape.dim] == [1, 20]
    assert document.opset_import[0].version == 17
    metadata = {item.key: item.value for item in document.metadata_props}
    assert metadata == history.history_metadata(
        parent_sha256=digest, temporal_training_performed=False
    )
    assert metadata["temporal_training_performed"] == "false"
    assert metadata["candidate_promoted"] == "false"
    assert receipt["onnx_runtime_parity_passed"] is True
    assert receipt["onnx_parity_probe_count"] == 3


def test_formal_cli_rejects_pending_data_without_creating_output(tmp_path: Path) -> None:
    _, dataset = _episodes(tmp_path)
    output = tmp_path / "must-not-exist"
    with pytest.raises(ValueError, match="not training-ready"):
        history.main(
            [
                "--parent-model",
                str(tmp_path / "parent.onnx"),
                "--parent-sha256",
                "0" * 64,
                "--output-directory",
                str(output),
                "--trajectory-dataset",
                str(dataset),
            ]
        )
    assert not output.exists()


def test_cli_training_and_scaffold_are_mutually_exclusive(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        history.main(
            [
                "--parent-model",
                "parent.onnx",
                "--parent-sha256",
                "0" * 64,
                "--output-directory",
                str(tmp_path / "output"),
                "--trajectory-dataset",
                "data.json",
                "--export-scaffold",
            ]
        )
    assert not (tmp_path / "output").exists()


def test_direct_script_help_works_without_loading_torch() -> None:
    result = subprocess.run(
        [sys.executable, str(Path(history.__file__).resolve()), "--help"],
        cwd=Path(history.__file__).resolve().parents[1],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "--export-scaffold" in result.stdout
    assert "--trajectory-dataset" in result.stdout


def test_scaffold_reports_no_training_or_promotion_and_never_creates_optimizer(
    parent_onnx, tmp_path: Path, monkeypatch
) -> None:
    torch = pytest.importorskip("torch")
    _, path, digest = parent_onnx

    def unexpected(*_args, **_kwargs):
        raise AssertionError("Scaffold cannot create an optimizer or train")

    monkeypatch.setattr(torch.optim, "AdamW", unexpected)
    monkeypatch.setattr(history, "train_history_model", unexpected)
    output = tmp_path / "scaffold"
    assert (
        history.main(
            [
                "--parent-model",
                str(path),
                "--parent-sha256",
                digest,
                "--output-directory",
                str(output),
                "--export-scaffold",
                "--device",
                "cpu",
            ]
        )
        == 0
    )
    report = json.loads((output / "training-report.json").read_text())
    assert report["method"] == "parent_weight_migration_only"
    for field in (
        "temporal_training_performed",
        "training_ready",
        "optimizer_created",
        "candidate_promoted",
        "final_release_allowed",
    ):
        assert report[field] is False
    assert report["epochs"] == 0
    assert report["metrics"] is None
    assert report["inherited_parameters"] == 15188
    assert (
        report["model_sha256"]
        == hashlib.sha256(Path(report["model_path"]).read_bytes()).hexdigest()
    )


def test_source_drift_prevents_scaffold_report(parent_onnx, tmp_path: Path, monkeypatch) -> None:
    _, path, digest = parent_onnx
    source = tmp_path / "fixture-source.py"
    source.write_text("before")
    monkeypatch.setattr(history, "_source_snapshot", lambda: {str(source): trainer._sha256(source)})
    original = history.export_history_onnx

    def export(*args, **kwargs):
        receipt = original(*args, **kwargs)
        source.write_text("after")
        return receipt

    monkeypatch.setattr(history, "export_history_onnx", export)
    output = tmp_path / "drifting-scaffold"
    with pytest.raises(ValueError, match="source changed"):
        history.main(
            [
                "--parent-model",
                str(path),
                "--parent-sha256",
                digest,
                "--output-directory",
                str(output),
                "--export-scaffold",
                "--device",
                "cpu",
            ]
        )
    assert not (output / "training-report.json").exists()
