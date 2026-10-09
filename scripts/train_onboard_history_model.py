"""Train a causal eight-frame extension of the existing OnboardAI policy.

This entry point consumes only the existing reviewed trajectory contract.  The
scaffold option migrates weights and exports the interface without training or
inventing action labels.  Neither path establishes flight or promotion evidence.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import onboard_sequence_windows as sequences
from scripts import train_onboard_model as trainer

HISTORY_STEPS = 8
FEATURE_WIDTH = 40
HIDDEN_WIDTH = 96
TEMPORAL_WIDTH = 64
OUTPUT_WIDTH = 20
DT_CLIP_S = 30.0
MAX_EPISODE_GAP_S = 5.0
PARENT_INITIALIZERS = 10
PARENT_PARAMETERS = 15188
CONTRACT = "multidetect.onboard-history-policy-v1"
INPUT_NAMES = ("features", "valid_mask", "dt_s", "reset_mask")


def history_metadata(*, parent_sha256: str, temporal_training_performed: bool) -> dict[str, str]:
    from multidetect.autonomy.onboard_history_onnx import onboard_history_metadata

    if len(parent_sha256) != 64 or any(c not in "0123456789abcdef" for c in parent_sha256):
        raise ValueError("History metadata requires a canonical parent SHA-256")
    if type(temporal_training_performed) is not bool:
        raise ValueError("temporal_training_performed must be boolean")
    return {
        **onboard_history_metadata(),
        "parent_model_sha256": parent_sha256,
        "temporal_training_performed": str(temporal_training_performed).lower(),
        "candidate_promoted": "false",
    }


@dataclass(frozen=True, slots=True)
class HistoryWindow:
    """A right-aligned prefix ending at one unique, labelled current step."""

    episode: sequences.TrajectoryEpisode
    steps: tuple[sequences.TrajectoryStep | None, ...]

    @property
    def current(self) -> sequences.TrajectoryStep:
        step = self.steps[-1]
        if step is None:
            raise ValueError("History window has no current observation")
        return step

    @property
    def features(self) -> tuple[tuple[float, ...], ...]:
        return tuple(
            (0.0,) * FEATURE_WIDTH if step is None else step.sample.features for step in self.steps
        )

    @property
    def valid_mask(self) -> tuple[float, ...]:
        return tuple(float(step is not None) for step in self.steps)

    @property
    def dt_s(self) -> tuple[float, ...]:
        return tuple(0.0 if step is None else step.dt_s for step in self.steps)

    @property
    def reset_mask(self) -> tuple[float, ...]:
        return tuple(float(step is not None and step.episode_reset) for step in self.steps)


def build_history_windows(
    episodes: Iterable[sequences.TrajectoryEpisode], *, require_reviewed: bool = True
) -> tuple[HistoryWindow, ...]:
    """Create exactly one causal window per transition; no future label is input.

    require_reviewed=False is solely an in-memory contract-fixture facility.
    Formal training always requires reviewed episodes and has no such CLI flag.
    """

    if type(require_reviewed) is not bool:
        raise ValueError("require_reviewed must be boolean")
    episodes = tuple(episodes)
    # Reuse all existing identity, ordering, source-split and feature checks.
    sequences.collate_episode_windows(episodes, window_size=HISTORY_STEPS)
    if require_reviewed and any(not episode.training_ready for episode in episodes):
        raise ValueError("Temporal training requires independently reviewed episodes")
    if any(
        len({step.context.home_position.canonical_hash for step in episode.steps}) != 1
        for episode in episodes
    ):
        raise ValueError("History episode home identity changed; a separate episode is required")
    if any(step.dt_s > MAX_EPISODE_GAP_S for episode in episodes for step in episode.steps):
        raise ValueError(
            "History episode gap exceeds 5 seconds; an actual episode boundary is required"
        )
    windows = []
    for episode in episodes:
        for end in range(len(episode.steps)):
            prefix = episode.steps[max(0, end + 1 - HISTORY_STEPS) : end + 1]
            windows.append(
                HistoryWindow(episode, ((None,) * (HISTORY_STEPS - len(prefix))) + prefix)
            )
    return tuple(windows)


def load_training_windows(path: Path) -> tuple[HistoryWindow, ...]:
    """The persisted-data admission boundary; never enables unreviewed loading."""

    windows = build_history_windows(sequences.load_trajectory_episodes(Path(path)))
    if {window.episode.identity.split for window in windows} != {
        "train",
        "evaluation",
        "test",
    }:
        raise ValueError("Temporal training needs independent train/evaluation/test episodes")
    return windows


def build_parent_model():
    """Return the exact parent-shaped module, keeping Torch import lazy."""

    import torch
    from torch import nn

    if (
        len(trainer.ONBOARD_MODEL_FEATURE_NAMES) != FEATURE_WIDTH
        or len(trainer.ONBOARD_MODEL_TASK_ACTION_ORDER) != 9
        or len(trainer.ONBOARD_MODEL_ACTION_ORDER) != 7
    ):
        raise ValueError("Existing OnboardAI contract no longer matches the r33 architecture")

    class ParentPolicyNet(nn.Module):
        def __init__(self):
            super().__init__()
            self.body = nn.Sequential(
                nn.Linear(FEATURE_WIDTH, HIDDEN_WIDTH),
                nn.ReLU(),
                nn.Linear(HIDDEN_WIDTH, HIDDEN_WIDTH),
                nn.ReLU(),
            )
            self.task = nn.Linear(HIDDEN_WIDTH, 9)
            self.flight = nn.Linear(HIDDEN_WIDTH, 7)
            self.regression = nn.Linear(HIDDEN_WIDTH, 4)

        def forward(self, features):
            hidden = self.body(features)
            return torch.cat(
                (self.task(hidden), self.flight(hidden), self.regression(hidden)), dim=1
            )

    return ParentPolicyNet()


def build_history_model(parent_model=None):
    """Encode each frame with the shared body and learn an order-sensitive residual.

    A kernel spanning all eight slots makes old/new order distinguishable.  The
    last projection starts at zero, so migration initially preserves the current
    frame's raw parent logits exactly.  No hidden/recurrent state crosses windows.
    """

    import torch
    from torch import nn

    parent = build_parent_model() if parent_model is None else parent_model
    reference = build_parent_model()
    if {name: tuple(value.shape) for name, value in parent.state_dict().items()} != {
        name: tuple(value.shape) for name, value in reference.state_dict().items()
    }:
        raise ValueError("History extension requires the exact parent-shaped module")

    class HistoryPolicyNet(nn.Module):
        def __init__(self):
            super().__init__()
            self.body = parent.body
            self.task = parent.task
            self.flight = parent.flight
            self.regression = parent.regression
            self.temporal_conv = nn.Conv1d(HIDDEN_WIDTH + 3, TEMPORAL_WIDTH, HISTORY_STEPS)
            self.temporal_projection = nn.Linear(TEMPORAL_WIDTH, HIDDEN_WIDTH)
            nn.init.zeros_(self.temporal_projection.weight)
            nn.init.zeros_(self.temporal_projection.bias)

        def forward(self, features, valid_mask, dt_s, reset_mask):
            valid = valid_mask.unsqueeze(-1)
            safe_features = torch.where(valid > 0.5, features, torch.zeros_like(features))
            encoded = self.body(safe_features) * valid
            safe_dt = torch.where(valid_mask > 0.5, dt_s, torch.zeros_like(dt_s))
            safe_reset = torch.where(valid_mask > 0.5, reset_mask, torch.zeros_like(reset_mask))
            elapsed = torch.clamp(safe_dt, min=0.0, max=DT_CLIP_S) / DT_CLIP_S
            temporal_input = torch.cat(
                (
                    encoded,
                    valid,
                    (elapsed * valid_mask).unsqueeze(-1),
                    (safe_reset * valid_mask).unsqueeze(-1),
                ),
                dim=2,
            )
            history = torch.relu(self.temporal_conv(temporal_input.transpose(1, 2)))[:, :, 0]
            hidden = encoded[:, -1, :] + self.temporal_projection(history)
            return torch.cat(
                (self.task(hidden), self.flight(hidden), self.regression(hidden)), dim=1
            )

    return HistoryPolicyNet()


def initialize_history_from_parent(model, parent_path: Path, expected_sha256: str) -> dict:
    """Use the existing strict initializer loader without relaxing its state dict."""

    import numpy as np
    import onnx
    import onnxruntime as ort
    import torch
    from onnx import numpy_helper

    parent_path = Path(parent_path).resolve()
    if trainer._sha256(parent_path) != expected_sha256:
        raise ValueError("parent model SHA-256 mismatch")
    parent_onnx = onnx.load(parent_path, load_external_data=False)
    if len(parent_onnx.graph.initializer) != PARENT_INITIALIZERS or any(
        item.data_type != onnx.TensorProto.FLOAT for item in parent_onnx.graph.initializer
    ):
        raise ValueError("Temporal migration requires exactly 10 float32 parent initializers")
    shapes = {
        item.name: numpy_helper.to_array(item).shape for item in parent_onnx.graph.initializer
    }
    if shapes.get("body.0.weight") != (HIDDEN_WIDTH, FEATURE_WIDTH):
        raise ValueError("Temporal migration requires the existing 40-feature parent")
    parent = build_parent_model()
    inheritance = trainer.initialize_from_parent(parent, parent_path, expected_sha256)
    if (
        inheritance["inherited_parameters"] != PARENT_PARAMETERS
        or inheritance["parent_feature_width"] != FEATURE_WIDTH
        or len(parent.state_dict()) != PARENT_INITIALIZERS
        or trainer._sha256(parent_path) != expected_sha256
    ):
        raise ValueError("Parent migration identity/parameter count changed")
    parent_session = ort.InferenceSession(str(parent_path), providers=["CPUExecutionProvider"])
    inputs, outputs = parent_session.get_inputs(), parent_session.get_outputs()
    if (
        len(inputs) != 1
        or inputs[0].name != "features"
        or inputs[0].type != "tensor(float)"
        or inputs[0].shape != [1, FEATURE_WIDTH]
        or len(outputs) != 1
        or outputs[0].name != "policy_output"
        or outputs[0].type != "tensor(float)"
        or outputs[0].shape != [1, OUTPUT_WIDTH]
    ):
        raise ValueError("Parent raw ONNX input/output contract differs")
    parent.eval()
    rng = np.random.default_rng(3318)
    max_parent_error = 0.0
    for _ in range(3):
        features = rng.normal(size=(1, FEATURE_WIDTH)).astype(np.float32)
        with torch.no_grad():
            reference = parent(torch.from_numpy(features)).numpy()
        actual = parent_session.run(["policy_output"], {"features": features})[0]
        if not np.isfinite(actual).all() or not np.allclose(
            reference, actual, rtol=1e-4, atol=1e-5
        ):
            raise ValueError("Strict parent weights do not reproduce raw ONNX outputs")
        max_parent_error = max(max_parent_error, float(np.max(np.abs(reference - actual))))
    if trainer._sha256(parent_path) != expected_sha256:
        raise ValueError("Parent model changed during raw ONNX verification")
    # Each inherited submodule is loaded strictly, including all bias tensors.
    for name in ("body", "task", "flight", "regression"):
        getattr(model, name).load_state_dict(getattr(parent, name).state_dict(), strict=True)
    if any(model.temporal_projection.weight.detach().flatten() != 0) or any(
        model.temporal_projection.bias.detach() != 0
    ):
        raise ValueError("Temporal migration requires a zero-initialized residual projection")
    return {
        **inheritance,
        "parent_initializer_count": PARENT_INITIALIZERS,
        "migration_strict": True,
        "temporal_residual_zero_initialized": True,
        "parent_raw_onnx_parity_passed": True,
        "parent_raw_onnx_max_absolute_error": max_parent_error,
    }


def windows_to_tensors(windows: Iterable[HistoryWindow], *, device="cpu") -> dict:
    """Collate only current labels, with predecessor intervals still in seconds."""

    import torch

    windows = tuple(windows)
    if not windows or any(len(window.steps) != HISTORY_STEPS for window in windows):
        raise ValueError("Tensor collation requires non-empty eight-step history windows")
    return {
        "features": torch.tensor([w.features for w in windows], dtype=torch.float32, device=device),
        "valid_mask": torch.tensor(
            [w.valid_mask for w in windows], dtype=torch.float32, device=device
        ),
        "dt_s": torch.tensor([w.dt_s for w in windows], dtype=torch.float32, device=device),
        "reset_mask": torch.tensor(
            [w.reset_mask for w in windows], dtype=torch.float32, device=device
        ),
        "task_labels": torch.tensor(
            [w.current.sample.task_label for w in windows], dtype=torch.long, device=device
        ),
        "flight_labels": torch.tensor(
            [w.current.sample.flight_label for w in windows], dtype=torch.long, device=device
        ),
        "regression_labels": torch.tensor(
            [w.current.sample.regression for w in windows], dtype=torch.float32, device=device
        ),
    }


def _forward(model, batch, indices=None):
    return model(
        *(batch[name] if indices is None else batch[name][indices] for name in INPUT_NAMES)
    )


def train_history_model(
    model,
    windows: Iterable[HistoryWindow],
    *,
    epochs: int,
    seed: int,
    device: str = "cpu",
    batch_size: int = 64,
    learning_rate: float = 0.0002,
    regression_weight: float = 0.2,
) -> dict:
    """Optimize reviewed current-step labels; evaluation/test never enter AdamW."""

    import torch
    from torch import nn

    for name, value in (("epochs", epochs), ("batch_size", batch_size)):
        if type(value) is not int or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    if type(seed) is not int or seed < 0:
        raise ValueError("seed must be a non-negative integer")
    if any(not math.isfinite(value) or value <= 0 for value in (learning_rate, regression_weight)):
        raise ValueError("Training weights must be finite and positive")
    windows = tuple(windows)
    episodes = tuple({w.episode.identity.episode_group_sha256: w.episode for w in windows}.values())
    expected = build_history_windows(episodes)
    if windows != expected:
        raise ValueError("Training windows must be the complete unique episode prefixes")
    partitions = {
        split: tuple(w for w in windows if w.episode.identity.split == split)
        for split in ("train", "evaluation", "test")
    }
    if any(not rows for rows in partitions.values()):
        raise ValueError("Temporal training needs independent train/evaluation/test episodes")
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    model.to(device)
    train_batch = windows_to_tensors(partitions["train"], device=device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate, weight_decay=1e-4)
    compatibility = torch.tensor(
        [
            [
                flight in trainer._TASK_FLIGHT_COMPATIBILITY[task]
                for flight in trainer.ONBOARD_MODEL_ACTION_ORDER
            ]
            for task in trainer.ONBOARD_MODEL_TASK_ACTION_ORDER
        ],
        device=device,
        dtype=torch.bool,
    )
    for epoch in range(epochs):
        model.train()
        generator = torch.Generator().manual_seed(seed + epoch)
        order = torch.randperm(len(partitions["train"]), generator=generator).to(device)
        accumulated = 0.0
        for start in range(0, len(order), batch_size):
            indices = order[start : start + batch_size]
            optimizer.zero_grad(set_to_none=True)
            output = _forward(model, train_batch, indices)
            task, flight = (
                train_batch["task_labels"][indices],
                train_batch["flight_labels"][indices],
            )
            joint = (output[:, :9].unsqueeze(2) + output[:, 9:16].unsqueeze(1)).masked_fill(
                ~compatibility.unsqueeze(0), -1e4
            )
            loss = (
                nn.functional.cross_entropy(output[:, :9], task)
                + nn.functional.cross_entropy(output[:, 9:16], flight)
                + 0.5 * nn.functional.cross_entropy(joint.flatten(1), task * 7 + flight)
                + regression_weight
                * nn.functional.smooth_l1_loss(
                    output[:, 16:], train_batch["regression_labels"][indices]
                )
            )
            loss.backward()
            optimizer.step()
            accumulated += float(loss.detach()) * len(indices)
        print(f"history epoch {epoch + 1}/{epochs} loss={accumulated / len(order):.6f}", flush=True)
    model.eval()
    metrics = {}
    with torch.no_grad():
        for split, rows in partitions.items():
            batch = windows_to_tensors(rows, device=device)
            outputs = torch.cat(
                [
                    _forward(model, batch, slice(start, start + batch_size))
                    for start in range(0, len(rows), batch_size)
                ]
            )
            metrics[split] = {
                "windows": len(rows),
                "episodes": len({w.episode.identity.episode_group_sha256 for w in rows}),
                "task_accuracy": float(
                    (outputs[:, :9].argmax(1) == batch["task_labels"]).float().mean()
                ),
                "flight_accuracy": float(
                    (outputs[:, 9:16].argmax(1) == batch["flight_labels"]).float().mean()
                ),
                "raw_regression_mae": float(
                    (outputs[:, 16:] - batch["regression_labels"]).abs().mean()
                ),
            }
    return metrics


def export_history_onnx(
    model, path: Path, *, parent_sha256: str, temporal_training_performed: bool = False
) -> dict:
    """Export the fixed four-input interface and verify native ONNX Runtime parity."""

    import numpy as np
    import onnx
    import onnxruntime as ort
    import torch

    path = Path(path)
    if path.exists():
        raise ValueError("Refusing to overwrite an existing history ONNX")
    metadata = history_metadata(
        parent_sha256=parent_sha256, temporal_training_performed=temporal_training_performed
    )
    model.cpu().eval()
    example = (
        torch.zeros((1, HISTORY_STEPS, FEATURE_WIDTH), dtype=torch.float32),
        torch.ones((1, HISTORY_STEPS), dtype=torch.float32),
        torch.zeros((1, HISTORY_STEPS), dtype=torch.float32),
        torch.zeros((1, HISTORY_STEPS), dtype=torch.float32),
    )
    with torch.no_grad():
        torch.onnx.export(
            model,
            example,
            path,
            input_names=list(INPUT_NAMES),
            output_names=["policy_output"],
            opset_version=17,
            do_constant_folding=True,
            dynamo=False,
        )
    document = onnx.load(path, load_external_data=False)
    onnx.helper.set_model_props(document, metadata)
    onnx.checker.check_model(document)
    expected_shapes = ((1, HISTORY_STEPS, FEATURE_WIDTH), *((1, HISTORY_STEPS),) * 3)
    if tuple(item.name for item in document.graph.input) != INPUT_NAMES:
        raise ValueError("Exported history model lost a required input")
    for item, shape in zip(document.graph.input, expected_shapes, strict=True):
        if (
            item.type.tensor_type.elem_type != onnx.TensorProto.FLOAT
            or tuple(dimension.dim_value for dimension in item.type.tensor_type.shape.dim) != shape
        ):
            raise ValueError("Exported history model input contract mismatch")
    output = document.graph.output
    if (
        len(output) != 1
        or output[0].name != "policy_output"
        or output[0].type.tensor_type.elem_type != onnx.TensorProto.FLOAT
        or tuple(d.dim_value for d in output[0].type.tensor_type.shape.dim) != (1, OUTPUT_WIDTH)
    ):
        raise ValueError("Exported history model output contract mismatch")
    onnx.save(document, path)
    session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    rng = np.random.default_rng(3341)
    max_error = 0.0
    for count in (1, 3, HISTORY_STEPS):
        arrays = {
            "features": rng.normal(size=(1, HISTORY_STEPS, FEATURE_WIDTH)).astype(np.float32),
            "valid_mask": np.array(
                [[0.0] * (HISTORY_STEPS - count) + [1.0] * count], dtype=np.float32
            ),
            "dt_s": rng.uniform(0.0, 60.0, (1, HISTORY_STEPS)).astype(np.float32),
            "reset_mask": np.zeros((1, HISTORY_STEPS), dtype=np.float32),
        }
        arrays["reset_mask"][0, HISTORY_STEPS - count] = 1
        with torch.no_grad():
            reference = model(*(torch.from_numpy(arrays[name]) for name in INPUT_NAMES)).numpy()
        actual = session.run(["policy_output"], arrays)[0]
        if not np.isfinite(actual).all() or not np.allclose(
            actual, reference, rtol=1e-4, atol=1e-5
        ):
            raise ValueError("History ONNX Runtime output does not match Torch")
        max_error = max(max_error, float(np.max(np.abs(actual - reference))))
    return {
        "onnx_opset": 17,
        "onnx_runtime_parity_passed": True,
        "onnx_runtime_max_absolute_error": max_error,
        "onnx_parity_probe_count": 3,
    }


def _source_snapshot() -> dict[str, str]:
    from multidetect.autonomy import onboard_history, onboard_history_onnx

    return {
        str(Path(module.__file__).resolve()): trainer._sha256(Path(module.__file__))
        for module in (trainer, sequences, onboard_history, onboard_history_onnx)
    } | {str(Path(__file__).resolve()): trainer._sha256(Path(__file__))}


def _verify_snapshot(
    sources: dict[str, str],
    parent: Path,
    parent_sha256: str,
    dataset: Path | None,
    dataset_sha256: str | None,
) -> None:
    for path, digest in sources.items():
        if trainer._sha256(Path(path)) != digest:
            raise ValueError("History training source changed; no report sealed")
    if trainer._sha256(parent) != parent_sha256:
        raise ValueError("History parent model changed; no report sealed")
    if dataset is not None and trainer._sha256(dataset) != dataset_sha256:
        raise ValueError("History trajectory dataset changed; no report sealed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-model", type=Path, required=True)
    parser.add_argument("--parent-sha256", required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--trajectory-dataset", type=Path)
    modes.add_argument("--export-scaffold", action="store_true")
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--seed", type=int, default=20261016)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--learning-rate", type=float, default=0.0002)
    parser.add_argument("--regression-weight", type=float, default=0.2)
    parser.add_argument("--device", default="auto", choices=("auto", "cpu", "cuda"))
    args = parser.parse_args(argv)
    output = args.output_directory.resolve()
    if output.exists():
        raise ValueError("History output directory must be new")
    if len(args.parent_sha256) != 64 or any(
        c not in "0123456789abcdef" for c in args.parent_sha256
    ):
        raise ValueError("--parent-sha256 must be a canonical SHA-256")
    if (
        args.epochs < 1
        or args.batch_size < 1
        or args.seed < 0
        or any(
            not math.isfinite(value) or value <= 0
            for value in (args.learning_rate, args.regression_weight)
        )
    ):
        raise ValueError("Invalid temporal training parameters")
    parent = args.parent_model.resolve()
    dataset = None if args.trajectory_dataset is None else args.trajectory_dataset.resolve()
    sources = _source_snapshot()
    dataset_sha256 = None if dataset is None else trainer._sha256(dataset)
    # Admission is deliberately before Torch, output creation and ONNX export.
    windows = None if dataset is None else load_training_windows(dataset)
    _verify_snapshot(sources, parent, args.parent_sha256, dataset, dataset_sha256)
    import torch

    torch.manual_seed(args.seed)
    device = "cuda" if args.device == "auto" and torch.cuda.is_available() else args.device
    if device == "auto":
        device = "cpu"
    model = build_history_model()
    inheritance = initialize_history_from_parent(model, parent, args.parent_sha256)
    metrics = None
    if windows is not None:
        metrics = train_history_model(
            model,
            windows,
            epochs=args.epochs,
            seed=args.seed,
            device=device,
            batch_size=args.batch_size,
            learning_rate=args.learning_rate,
            regression_weight=args.regression_weight,
        )
        if load_training_windows(dataset) != windows:
            raise ValueError("Trajectory admission changed during temporal training")
    _verify_snapshot(sources, parent, args.parent_sha256, dataset, dataset_sha256)
    output.mkdir(parents=True)
    onnx_path = output / "onboard_history_policy.onnx"
    exported = export_history_onnx(
        model,
        onnx_path,
        parent_sha256=args.parent_sha256,
        temporal_training_performed=windows is not None,
    )
    _verify_snapshot(sources, parent, args.parent_sha256, dataset, dataset_sha256)
    report = {
        "schema_version": 1,
        "contract": CONTRACT,
        "method": "parent_weight_migration_only"
        if windows is None
        else "reviewed_trajectory_history_finetune",
        "temporal_training_performed": windows is not None,
        "optimizer_created": windows is not None,
        "training_ready": windows is not None,
        "synthetic_bootstrap": False,
        "generated_action_labels": False,
        "history_steps": HISTORY_STEPS,
        "feature_names": list(trainer.ONBOARD_MODEL_FEATURE_NAMES),
        "task_action_order": [item.value for item in trainer.ONBOARD_MODEL_TASK_ACTION_ORDER],
        "flight_action_order": [item.value for item in trainer.ONBOARD_MODEL_ACTION_ORDER],
        "input_names": list(INPUT_NAMES),
        "input_dtype": "float32",
        "input_shapes": {
            "features": [1, HISTORY_STEPS, FEATURE_WIDTH],
            **{name: [1, HISTORY_STEPS] for name in INPUT_NAMES[1:]},
        },
        "output_name": "policy_output",
        "output_shape": [1, OUTPUT_WIDTH],
        "window_policy": "one_unique_prefix_ending_at_each_current_transition_right_aligned",
        "label_policy": "current_slot_only",
        "future_observation_inputs": False,
        "padding_policy": "zero_features_dt_reset_hidden_and_valid_mask",
        "reset_policy": "actual_episode_origin_only_not_truncated_window_origin",
        "dt_units": "seconds",
        "dt_model_clip_s": DT_CLIP_S,
        "dt_model_scale_s": DT_CLIP_S,
        "training_max_episode_gap_s": MAX_EPISODE_GAP_S,
        "temporal_architecture": "shared_frame_body_full_window_conv1d_relu_residual",
        "model_path": str(onnx_path),
        "model_sha256": trainer._sha256(onnx_path),
        "model_bytes": onnx_path.stat().st_size,
        "parent_model_path": str(parent),
        **inheritance,
        **exported,
        "source_hashes": sources,
        "trajectory_dataset": None
        if dataset is None
        else {"path": str(dataset), "sha256": dataset_sha256},
        "epochs": 0 if windows is None else args.epochs,
        "seed": args.seed,
        "batch_size": args.batch_size,
        "learning_rate": args.learning_rate,
        "regression_weight": args.regression_weight,
        "device": device,
        "torch_version": torch.__version__,
        "metrics": metrics,
        "candidate_promoted": False,
        "final_release_allowed": False,
        "physical_performance_claimed": False,
        "hil_real_evidence": False,
        "jetson_evidence": False,
        "outdoor_gnss_evidence": False,
        "qualified_gazebo_evidence": False,
    }
    (output / "training-report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
