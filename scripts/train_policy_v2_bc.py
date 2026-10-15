"""Train the policy-v2 primitive baseline from SIM teacher episodes.

The script is deliberately conservative about data admission.  Only episodes
recorded with ``sensor_status=live`` and real RGB PNG/depth NPY, NPZ, or PNG16 files are
accepted.  ``labels`` are read only for optional audit metadata; state inputs
are always built from ``obs_before`` through :class:`StructuredSceneStateBuilder`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    import numpy as np
    import torch
    from torch import Tensor
    from torch.utils.data import Dataset
except ImportError:  # pragma: no cover - optional training environment
    np = None  # type: ignore[assignment]
    torch = None  # type: ignore[assignment]
    Tensor = Any  # type: ignore[misc,assignment]

from multidetect.autonomy.policy_v2 import StructuredSceneStateBuilder
from multidetect.autonomy.policy_v2.model import (
    ACTION_KINDS,
    PrimitivePolicy,
    export_onnx,
    states_to_tensors,
)
from multidetect.sim_teacher.recording import load_depth_array


class DatasetAdmissionError(ValueError):
    """Raised when a dataset does not meet the live sensor admission gate."""


DEFAULT_MAX_SIM_SKEW_MS = 250.0


def _sim_skew_values(row: dict[str, Any]) -> list[float]:
    values: list[float] = []
    for side in ("before", "after"):
        observation = row.get("obs_" + side)
        if not isinstance(observation, dict):
            continue
        value = observation.get("sim_skew_ms")
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        ):
            values.append(float(value))
    return values


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _real_rgb(ep: Path, value: object) -> bool:
    if not isinstance(value, str) or Path(value).is_absolute():
        return False
    path = ep / value.replace("\\", "/")
    try:
        with path.open("rb") as stream:
            return (
                path.suffix.lower() == ".png"
                and stream.read(8) == b"\x89PNG\r\n\x1a\n"
                and path.stat().st_size > 8
            )
    except OSError:
        return False


def _real_depth(ep: Path, value: object) -> bool:
    if not isinstance(value, str) or Path(value).is_absolute():
        return False
    path = ep / value.replace("\\", "/")
    if (
        path.suffix.lower() not in {".npy", ".npz", ".png"}
        or not path.is_file()
        or path.stat().st_size <= 0
    ):
        return False
    try:
        if np is None:
            return True
        array = load_depth_array(path)
        return bool(array.size and np.isfinite(array).all())
    except Exception:
        return False


def _admit_episode(
    ep: Path,
    *,
    max_sim_skew_ms: float = DEFAULT_MAX_SIM_SKEW_MS,
    skip_over_skew: bool = True,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        metadata = json.loads((ep / "episode.json").read_text(encoding="utf-8"))
    except Exception as exc:
        raise DatasetAdmissionError(f"invalid episode metadata: {ep.name}") from exc
    if metadata.get("sensor_status") != "live":
        raise DatasetAdmissionError(f"episode {ep.name} is not sensor_status=live")
    steps_path = ep / "steps.jsonl"
    if not steps_path.is_file():
        raise DatasetAdmissionError(f"episode {ep.name} lacks steps.jsonl")
    rows: list[dict[str, Any]] = []
    skipped_sim_skew_steps = 0
    perception_by_key: dict[tuple[int, str], dict[str, Any]] = {}
    perception_path = ep / "perception.jsonl"
    if perception_path.is_file():
        for line_no, line in enumerate(perception_path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                perception = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DatasetAdmissionError(f"invalid perception JSON {ep.name}:{line_no}") from exc
            if not isinstance(perception, dict):
                raise DatasetAdmissionError(f"invalid perception row {ep.name}:{line_no}")
            try:
                key = (
                    int(perception.get("step_index", -1)),
                    str(perception.get("observation", "")),
                )
            except (TypeError, ValueError) as exc:
                raise DatasetAdmissionError(f"invalid perception key {ep.name}:{line_no}") from exc
            if key[0] >= 0 and key[1] in {"before", "after"}:
                perception_by_key[key] = perception
    for line_no, line in enumerate(steps_path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DatasetAdmissionError(f"invalid JSON {ep.name}:{line_no}") from exc
        before, after = row.get("obs_before"), row.get("obs_after")
        # A live training transition must have both observations and actual files.
        if not isinstance(before, dict) or not isinstance(after, dict):
            raise DatasetAdmissionError(f"live episode {ep.name} has missing observations")
        for label, obs in (("before", before), ("after", after)):
            if not _real_rgb(ep, obs.get("rgb_path")) or not _real_depth(ep, obs.get("depth_path")):
                raise DatasetAdmissionError(
                    f"live episode {ep.name} has no real RGB/depth ({label})"
                )
        if not isinstance(row.get("action"), dict):
            raise DatasetAdmissionError(f"live episode {ep.name} has no action")
        if skip_over_skew and any(
            abs(value) > float(max_sim_skew_ms) for value in _sim_skew_values(row)
        ):
            skipped_sim_skew_steps += 1
            continue
        step_index = int(row.get("step_index", len(rows)))
        for observation_name, observation in (("before", before), ("after", after)):
            perception = perception_by_key.get((step_index, observation_name))
            if perception is not None:
                observation["perception"] = perception
        rows.append(row)
    metadata["sim_skew_skipped_steps"] = skipped_sim_skew_steps
    if not rows:
        if skipped_sim_skew_steps and skip_over_skew:
            return metadata, rows
        raise DatasetAdmissionError(f"episode {ep.name} contains no steps")
    return metadata, rows


def load_episodes(
    root: str | Path,
    *,
    max_sim_skew_ms: float = DEFAULT_MAX_SIM_SKEW_MS,
    skip_over_skew: bool = True,
) -> list[tuple[str, Path, dict[str, Any], list[dict[str, Any]]]]:
    """Load and strictly admit all episodes below ``root/episodes``.

    Returning an episode at a time makes it impossible for a row-level random
    split to leak adjacent windows between train and validation.
    """
    if not math.isfinite(float(max_sim_skew_ms)) or float(max_sim_skew_ms) < 0.0:
        raise ValueError("max_sim_skew_ms must be finite and non-negative")
    root_path = Path(root)
    episodes_dir = root_path / "episodes"
    if not episodes_dir.is_dir():
        raise DatasetAdmissionError(f"episodes directory missing: {episodes_dir}")
    result = []
    for ep in sorted(path for path in episodes_dir.iterdir() if path.is_dir()):
        metadata, rows = _admit_episode(
            ep, max_sim_skew_ms=max_sim_skew_ms, skip_over_skew=skip_over_skew
        )
        if rows:
            result.append((ep.name, ep, metadata, rows))
    if not result:
        raise DatasetAdmissionError("no episodes found")
    return result


# Public descriptive alias used by small integration harnesses.
load_dataset = load_episodes


def split_episodes(
    episodes: list[tuple[str, Path, dict[str, Any], list[dict[str, Any]]]], *, seed: int = 0
) -> dict[str, list[tuple[str, Path, dict[str, Any], list[dict[str, Any]]]]]:
    """Whole-world/episode split performed before state windowing."""
    groups: dict[str, list[tuple[str, Path, dict[str, Any], list[dict[str, Any]]]]] = {}
    for item in episodes:
        episode_id, _path, metadata, _rows = item
        world = metadata.get("world_id", metadata.get("world", episode_id))
        groups.setdefault(str(world), []).append(item)
    keys = sorted(groups)
    random.Random(seed).shuffle(keys)
    if len(keys) < 3:
        # Keep a deterministic non-empty train split for tiny smoke datasets.
        n_train = max(1, len(keys) - 1)
        n_val = 1 if len(keys) > 1 else 0
    else:
        n_train = max(1, int(round(0.70 * len(keys))))
        n_val = max(1, int(round(0.15 * len(keys))))
        if n_train + n_val >= len(keys):
            n_val = max(1, len(keys) - n_train - 1)
    train_keys, val_keys = keys[:n_train], keys[n_train : n_train + n_val]
    test_keys = keys[n_train + n_val :]
    return {
        "train": [item for key in train_keys for item in groups[key]],
        "validation": [item for key in val_keys for item in groups[key]],
        "test": [item for key in test_keys for item in groups[key]],
    }


@dataclass
class Transition:
    state: Any
    kind: int
    continuous: tuple[float, float, float, float, float]
    mask: tuple[float, float, float, float]
    weight: float


class _TransitionDataset(Dataset[Transition]):
    def __init__(self, rows: list[Transition]):
        self.rows = rows

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, index: int) -> Transition:
        return self.rows[index]


def _action_target(action: dict[str, Any]) -> tuple[int, tuple[float, ...], tuple[float, ...]]:
    kind = action.get("kind")
    if kind not in ACTION_KINDS:
        raise DatasetAdmissionError(f"unsupported teacher action kind: {kind!r}")
    values = tuple(
        float(action.get(name, 0.0))
        for name in ("forward_m", "right_m", "up_m", "yaw_delta_deg", "seconds")
    )
    if not all(math.isfinite(value) for value in values):
        raise DatasetAdmissionError("teacher action has non-finite values")
    mask_value = action.get("action_mask", [True] * 4)
    if not isinstance(mask_value, (list, tuple)) or len(mask_value) != 4:
        raise DatasetAdmissionError("teacher action has invalid action_mask")
    mask = tuple(1.0 if bool(value) else 0.0 for value in mask_value)
    return ACTION_KINDS.index(kind), values, mask


def build_transitions(
    split: list[tuple[str, Path, dict[str, Any], list[dict[str, Any]]]],
) -> list[Transition]:
    result: list[Transition] = []
    for _episode_id, _ep, _metadata, rows in split:
        builder = StructuredSceneStateBuilder()
        for row in rows:
            state = builder.from_sim_step_record(row)
            kind, values, mask = _action_target(row["action"])
            outcome = row.get("outcome") or {}
            status = str(outcome.get("status", "")).lower()
            weight = 0.25 if status in {"timeout", "rejected"} else 1.0
            result.append(Transition(state, kind, values, mask, weight))
    return result


def _batch(items: list[Transition]) -> tuple[Tensor, Tensor, Tensor, Tensor, Tensor, Tensor]:
    states, memories = [], []
    for item in items:
        state, memory = states_to_tensors(item.state)
        states.append(state[0])
        memories.append(memory[0])
    return (
        torch.stack(states),
        torch.stack(memories),
        torch.tensor([item.mask for item in items], dtype=torch.float32),
        torch.tensor([item.kind for item in items], dtype=torch.long),
        torch.tensor([item.continuous for item in items], dtype=torch.float32),
        torch.tensor([item.weight for item in items], dtype=torch.float32),
    )


def train_model(
    train: list[Transition],
    validation: list[Transition],
    *,
    epochs: int,
    batch_size: int,
    seed: int,
    device: str,
) -> tuple[Any, dict[str, Any]]:
    if torch is None:
        raise RuntimeError("PyTorch is required for policy-v2 BC training")
    torch.manual_seed(seed)
    model = PrimitivePolicy().to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-3)
    history: list[dict[str, float]] = []
    for epoch in range(epochs):
        model.train()
        random.Random(seed + epoch).shuffle(train)
        total = 0.0
        count = 0
        for start in range(0, len(train), batch_size):
            batch = train[start : start + batch_size]
            state, memory, mask, kind, target, weight = _batch(batch)
            state, memory, mask, kind, target, weight = [
                x.to(device) for x in (state, memory, mask, kind, target, weight)
            ]
            logits, continuous, _value, _uncertainty = model(state, memory, mask)
            ce = torch.nn.functional.cross_entropy(logits, kind, reduction="none")
            # Regression is only supervised on the dimensions meaningful for the selected kind.
            active = torch.zeros_like(target)
            active[:, 0:3] = (kind == 0).float().unsqueeze(1)
            active[:, 3] = (kind == 1).float()
            active[:, 4] = (kind == 2).float()
            reg = torch.nn.functional.smooth_l1_loss(continuous, target, reduction="none")
            reg = (reg * active).sum(dim=1) / active.sum(dim=1).clamp_min(1.0)
            stop_loss = torch.nn.functional.binary_cross_entropy_with_logits(
                logits[:, 3], (kind == 3).float(), reduction="none"
            )
            loss = ((ce + reg + 0.2 * stop_loss) * weight).mean()
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            total += float(loss.item()) * len(batch)
            count += len(batch)
        history.append({"epoch": epoch + 1, "train_loss": total / max(1, count)})
    model.eval()
    with torch.no_grad():
        values = []
        for start in range(0, len(validation), batch_size):
            batch = validation[start : start + batch_size]
            if not batch:
                continue
            state, memory, mask, kind, target, weight = _batch(batch)
            logits, continuous, _v, _u = model(state.to(device), memory.to(device), mask.to(device))
            values.append(float(torch.nn.functional.cross_entropy(logits, kind.to(device)).item()))
    metrics = {"history": history, "validation_ce": sum(values) / len(values) if values else None}
    return model, metrics


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--epochs", type=int, default=2)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--device", default="cpu")
    parser.add_argument(
        "--max-sim-skew-ms",
        type=float,
        default=DEFAULT_MAX_SIM_SKEW_MS,
        help="skip steps whose observation sim_skew_ms exceeds this limit (default: 250)",
    )
    parser.add_argument(
        "--no-skip-over-skew",
        action="store_true",
        help="retain steps over the simulation-skew limit",
    )
    args = parser.parse_args(argv)
    if torch is None:
        parser.error("PyTorch is required; install a training environment with torch")
    episodes = load_episodes(
        args.dataset_root,
        max_sim_skew_ms=args.max_sim_skew_ms,
        skip_over_skew=not args.no_skip_over_skew,
    )
    splits = split_episodes(episodes, seed=args.seed)
    transitions = {name: build_transitions(items) for name, items in splits.items()}
    if not transitions["train"]:
        raise DatasetAdmissionError("training split is empty")
    model, metrics = train_model(
        transitions["train"],
        transitions["validation"],
        epochs=args.epochs,
        batch_size=args.batch_size,
        seed=args.seed,
        device=args.device,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = args.output_dir / "policy_v2_bc.pt"
    torch.save({"state_dict": model.state_dict(), "config": {"seed": args.seed}}, checkpoint)
    onnx_path = args.output_dir / "policy_v2_bc.onnx"
    # The fixed export sample is CPU; move the trained module explicitly so a
    # CUDA training run produces the same portable artifact.
    model = model.cpu()
    export_onnx(model, onnx_path)
    manifest = {
        str(path.relative_to(args.dataset_root)): _sha256(path)
        for _id, path, _meta, _rows in episodes
        for path in (path / "episode.json", path / "steps.jsonl")
    }
    report = {
        "schema": "multidetect.policy-v2-bc-report-v1",
        "config": {
            "epochs": args.epochs,
            "batch_size": args.batch_size,
            "seed": args.seed,
            "device": args.device,
            "max_sim_skew_ms": args.max_sim_skew_ms,
            "skip_over_skew": not args.no_skip_over_skew,
            "skipped_sim_skew_steps": sum(
                int(metadata.get("sim_skew_skipped_steps", 0))
                for _id, _path, metadata, _rows in episodes
            ),
        },
        "split": {name: [item[0] for item in value] for name, value in splits.items()},
        "counts": {name: len(value) for name, value in transitions.items()},
        "data_hashes": manifest,
        "metrics": metrics,
        "checkpoint": checkpoint.name,
        "onnx": onnx_path.name,
    }
    (args.output_dir / "report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
