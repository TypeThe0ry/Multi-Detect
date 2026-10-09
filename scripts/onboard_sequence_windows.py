"""Episode-preserving inputs for a future temporal OnboardAI training run.

The input is the existing ``multidetect.onboard-trajectory-dataset-v1``
contract, not a clip/renderer schema.  Labels and BootstrapSample objects come
from train_onboard_model._trajectory_samples; this module never creates expert
actions.  It does not train a model, change the current MLP, or establish
Gazebo, flight, or hardware success.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from scripts import train_onboard_model as trainer

# The shared loader initializes the checkout's src import path.
# isort: split

from multidetect.autonomy.contracts import ContextSnapshot
from multidetect.autonomy.onboard_agent import OnboardModelInput, OnboardRuntimeEvent
from multidetect.autonomy.serialization import (
    canonical_json,
    require_bool,
    require_int,
    require_real,
    require_sha256,
    require_text,
    strict_json_loads,
)


@dataclass(frozen=True, slots=True)
class EpisodeIdentity:
    """Existing trajectory identity; task contract scopes the continuous mission.

    executable_mission_sha256 belongs to each step's label proof and can change
    during legitimate replanning.  It is not a substitute for this identity.
    """

    episode_id: str
    scene_id: str
    scenario_manifest_sha256: str
    airframe_profile_id: str
    airframe_profile_sha256: str
    recording_id: str
    recording_hash: str
    scenario_seed: int
    task_contract_sha256: str
    episode_group_sha256: str
    seed: int
    split: str


@dataclass(frozen=True, slots=True)
class TrajectoryStep:
    identity: EpisodeIdentity
    sample: trainer.BootstrapSample
    context: ContextSnapshot
    event: OnboardRuntimeEvent
    transition_id: str
    frame_id: str
    next_frame_id: str | None
    sequence: int
    observed_at_s: float
    dt_s: float
    episode_reset: bool
    executable_mission_sha256: str


@dataclass(frozen=True, slots=True)
class TrajectoryEpisode:
    """Immutable view of an episode validated by the existing trajectory loader."""

    identity: EpisodeIdentity
    steps: tuple[TrajectoryStep, ...]
    dataset_sha256: str
    training_ready: bool


@dataclass(frozen=True, slots=True)
class SequenceWindow:
    """One independent window, with trailing padding represented by None.

    padding_mask=True means padding (ignore in attention/loss).  reset_mask
    marks the actual episode origin, not every slice origin.  Each independent
    window additionally requires reset_before_window=True; carrying recurrent
    state between overlapping or shuffled windows would be incorrect.  dt_s
    retains the original predecessor interval even at a nonzero slice origin.
    Padding has dt=0, reset=False, and no sample/action labels.
    """

    episode: TrajectoryEpisode
    steps: tuple[TrajectoryStep | None, ...]
    reset_before_window: bool = True

    @property
    def identity(self) -> EpisodeIdentity:
        return self.episode.identity

    @property
    def padding_mask(self) -> tuple[bool, ...]:
        return tuple(step is None for step in self.steps)

    @property
    def reset_mask(self) -> tuple[bool, ...]:
        return tuple(step is not None and step.episode_reset for step in self.steps)

    @property
    def dt_s(self) -> tuple[float, ...]:
        return tuple(0.0 if step is None else step.dt_s for step in self.steps)

    @property
    def samples(self) -> tuple[trainer.BootstrapSample | None, ...]:
        return tuple(None if step is None else step.sample for step in self.steps)

    @property
    def task_labels(self) -> tuple[int | None, ...]:
        return tuple(None if step is None else step.sample.task_label for step in self.steps)

    @property
    def flight_labels(self) -> tuple[int | None, ...]:
        return tuple(None if step is None else step.sample.flight_label for step in self.steps)


def _strict_document(payload: bytes, name: str) -> dict:
    document = strict_json_loads(payload, name=name)
    if not isinstance(document, dict):
        raise ValueError(f"{name} must contain an object")
    # Also rejects numeric overflow (e.g. 1e999), including unused metadata.
    canonical_json(document)
    return document


def load_trajectory_episodes(
    path: Path, *, allow_unreviewed: bool = False
) -> tuple[TrajectoryEpisode, ...]:
    """Restore episode/time metadata around the existing verified sample rows.

    The default enforces all current materialized-source, raw-recording,
    independent-review and planner-proof checks.  The explicit unreviewed
    opt-in has exactly the existing loader's meaning; it still requires labels
    and never accepts pending-label staging or renderer planned conditions.
    Any explicit opt-in keeps the returned training_ready flag false, even
    when the input declares readiness and passes the existing proof checks.
    """

    require_bool(allow_unreviewed, "allow_unreviewed")
    path = Path(path).resolve()
    payload = path.read_bytes()
    document = _strict_document(payload, "Onboard trajectory dataset")
    receipt = document.get("review_receipt")
    if not isinstance(receipt, dict):
        raise ValueError("Onboard trajectory review receipt is missing")
    receipt_path = trainer._resolve_dataset_file(path, receipt.get("path"), "review receipt")
    receipt_payload = receipt_path.read_bytes()
    _strict_document(receipt_payload, "Onboard trajectory review receipt")
    # Do not replicate or weaken the validated transition/action/proof parser.
    samples = trainer._trajectory_samples(path, allow_unreviewed=allow_unreviewed)
    if path.read_bytes() != payload or receipt_path.read_bytes() != receipt_payload:
        raise ValueError("Onboard trajectory dataset/review receipt changed during validation")
    dataset_hash = hashlib.sha256(payload).hexdigest()
    episodes = []
    cursor = 0
    for raw_episode in document["episodes"]:
        identity = EpisodeIdentity(
            **{field: raw_episode[field] for field in EpisodeIdentity.__dataclass_fields__}
        )
        steps = []
        previous_time = None
        for raw_step in raw_episode["transitions"]:
            if cursor >= len(samples):
                raise ValueError("Onboard trajectory parser row count does not match episodes")
            sample = samples[cursor]
            cursor += 1
            observed_at_s = require_real(raw_step["observed_at_s"], "observed_at_s", minimum=0)
            features = tuple(require_real(value, "feature") for value in raw_step["features"])
            regression = tuple(
                require_real(value, "regression") for value in raw_step["regression"]
            )
            if (
                sample.features != features
                or sample.regression != regression
                or sample.task_label != raw_step["task_label"]
                or sample.flight_label != raw_step["flight_label"]
            ):
                raise ValueError(
                    "Onboard trajectory parser row does not match persisted transition"
                )
            observation = raw_step["observation"]
            steps.append(
                TrajectoryStep(
                    identity=identity,
                    sample=sample,
                    context=ContextSnapshot.from_mapping(observation["context"]),
                    event=trainer._onboard_event_from_mapping(observation["event"]),
                    transition_id=raw_step["transition_id"],
                    frame_id=raw_step["frame_id"],
                    next_frame_id=raw_step["next_frame_id"],
                    sequence=raw_step["sequence"],
                    observed_at_s=observed_at_s,
                    dt_s=0.0 if previous_time is None else observed_at_s - previous_time,
                    episode_reset=previous_time is None,
                    executable_mission_sha256=raw_step["label_proof"]["executable_mission_sha256"],
                )
            )
            previous_time = observed_at_s
        episode = TrajectoryEpisode(
            identity,
            tuple(steps),
            dataset_hash,
            document.get("training_ready") is True and not allow_unreviewed,
        )
        _validate_episode(episode)
        episodes.append(episode)
    if cursor != len(samples):
        raise ValueError("Onboard trajectory parser row count does not match episodes")
    # Construction also reads the canonical observations; seal both reads.
    if path.read_bytes() != payload or receipt_path.read_bytes() != receipt_payload:
        raise ValueError("Onboard trajectory dataset/review receipt changed during collation")
    return tuple(episodes)


def _validate_episode(episode: TrajectoryEpisode) -> None:
    """Check window-critical invariants again for in-memory joins/subsets."""

    if (
        not isinstance(episode, TrajectoryEpisode)
        or not isinstance(episode.steps, tuple)
        or not episode.steps
    ):
        raise ValueError("Collation requires non-empty validated TrajectoryEpisode objects")
    identity = episode.identity
    if not isinstance(identity, EpisodeIdentity):
        raise ValueError("Onboard trajectory episode identity is invalid")
    require_sha256(episode.dataset_sha256, "dataset_sha256")
    require_bool(episode.training_ready, "training_ready")
    require_int(identity.seed, "seed", minimum=0)
    require_int(identity.scenario_seed, "scenario_seed", minimum=0)
    for field in ("episode_id", "scene_id", "airframe_profile_id", "recording_id"):
        require_text(getattr(identity, field), field)
    for field in (
        "scenario_manifest_sha256",
        "airframe_profile_sha256",
        "recording_hash",
        "task_contract_sha256",
        "episode_group_sha256",
    ):
        value = getattr(identity, field)
        if require_sha256(value, field) != value:
            raise ValueError(f"Onboard trajectory identity {field} is not canonical")
    group_hash = hashlib.sha256(
        "|".join(
            (
                identity.recording_hash,
                identity.scenario_manifest_sha256,
                identity.airframe_profile_sha256,
                str(identity.scenario_seed),
                identity.task_contract_sha256,
            )
        ).encode("utf-8")
    ).hexdigest()
    if identity.episode_group_sha256 != group_hash:
        raise ValueError("Onboard trajectory mission/episode group identity mismatch")
    if identity.split not in {"train", "evaluation", "test"}:
        raise ValueError("Onboard trajectory episode split is invalid")
    previous_time = None
    seen_ids: set[str] = set()
    seen_frames: set[str] = set()
    for index, step in enumerate(episode.steps):
        if not isinstance(step, TrajectoryStep) or step.identity != identity:
            raise ValueError("Onboard trajectory step crosses mission/episode identity")
        if require_int(step.sequence, "sequence", minimum=0) != index:
            raise ValueError("Onboard trajectory steps must remain in contiguous episode order")
        require_text(step.transition_id, "transition_id")
        require_text(step.frame_id, "frame_id")
        if step.transition_id in seen_ids or step.frame_id in seen_frames:
            raise ValueError("Onboard trajectory transition/frame identity is reused")
        seen_ids.add(step.transition_id)
        seen_frames.add(step.frame_id)
        expected_next = (
            episode.steps[index + 1].frame_id if index + 1 < len(episode.steps) else None
        )
        if step.next_frame_id != expected_next:
            raise ValueError("Onboard trajectory next frame binding mismatch")
        timestamp = require_real(step.observed_at_s, "observed_at_s", minimum=0)
        dt = require_real(step.dt_s, "dt_s", minimum=0)
        if previous_time is not None and timestamp <= previous_time:
            raise ValueError("Onboard trajectory timestamps must remain strictly increasing")
        expected_dt = 0.0 if previous_time is None else timestamp - previous_time
        if dt != expected_dt or require_bool(step.episode_reset, "episode_reset") != (index == 0):
            raise ValueError("Onboard trajectory dt/reset boundary mismatch")
        previous_time = timestamp
        sample = step.sample
        if (
            not isinstance(sample, trainer.BootstrapSample)
            or sample.group_id != f"trajectory:{identity.episode_group_sha256}"
            or sample.split != identity.split
        ):
            raise ValueError("Onboard trajectory sample crosses mission/episode/split identity")
        require_int(
            sample.task_label,
            "task_label",
            minimum=0,
            maximum=len(trainer.ONBOARD_MODEL_TASK_ACTION_ORDER) - 1,
        )
        require_int(
            sample.flight_label,
            "flight_label",
            minimum=0,
            maximum=len(trainer.ONBOARD_MODEL_ACTION_ORDER) - 1,
        )
        if not isinstance(sample.features, tuple) or not isinstance(sample.regression, tuple):
            raise ValueError("Onboard trajectory sample vectors must remain immutable tuples")
        if len(sample.features) != len(trainer.ONBOARD_MODEL_FEATURE_NAMES):
            raise ValueError("Onboard trajectory feature width mismatch")
        for value in (*sample.features, *sample.regression):
            require_real(value, "sample value")
        if len(sample.regression) != 4:
            raise ValueError("Onboard trajectory regression width mismatch")
        if not isinstance(step.context, ContextSnapshot) or not isinstance(
            step.event, OnboardRuntimeEvent
        ):
            raise ValueError("Onboard trajectory canonical context/event objects are missing")
        if (
            step.context.airframe_profile_id != identity.airframe_profile_id
            or step.context.airframe_profile_hash not in {None, identity.airframe_profile_sha256}
            or not math.isclose(step.context.captured_at_s, timestamp, rel_tol=0, abs_tol=1e-9)
            or not math.isclose(step.event.observed_at_s, timestamp, rel_tol=0, abs_tol=1e-9)
        ):
            raise ValueError("Onboard trajectory canonical observation identity/time mismatch")
        expected_features = OnboardModelInput(step.context, step.event).feature_vector()
        if any(
            not math.isclose(left, right, rel_tol=0, abs_tol=1e-12)
            for left, right in zip(sample.features, expected_features, strict=True)
        ):
            raise ValueError("Onboard trajectory feature recomputation mismatch")
        task_action = trainer.ONBOARD_MODEL_TASK_ACTION_ORDER[sample.task_label]
        flight_action = trainer.ONBOARD_MODEL_ACTION_ORDER[sample.flight_label]
        if flight_action not in trainer._TASK_FLIGHT_COMPATIBILITY[task_action]:
            raise ValueError("Onboard trajectory action label pair is incompatible")
        require_sha256(step.executable_mission_sha256, "executable_mission_sha256")


def collate_episode_windows(
    episodes: Iterable[TrajectoryEpisode], *, window_size: int, stride: int | None = None
) -> tuple[SequenceWindow, ...]:
    """Collate loaded episodes/subsets without shuffling, crossing, or dropping steps.

    Default stride=window_size produces disjoint windows with a padded tail.
    Smaller positive strides produce independent overlapping windows.  A stride
    larger than the window is rejected because it would silently drop steps.
    This is an in-memory collator, not an alternative artifact/proof validator;
    load_trajectory_episodes is the admission boundary for persisted datasets.
    """

    window_size = require_int(window_size, "window_size", minimum=1)
    stride = window_size if stride is None else require_int(stride, "stride", minimum=1)
    if stride > window_size:
        raise ValueError("stride cannot exceed window_size and drop transitions")
    episodes = tuple(episodes)
    if not episodes:
        raise ValueError("Collation requires at least one episode")
    groups: set[str] = set()
    frames: set[str] = set()
    splits: dict[tuple[str, str], str] = {}
    windows = []
    for episode in episodes:
        _validate_episode(episode)
        identity = episode.identity
        if identity.episode_group_sha256 in groups:
            raise ValueError("Onboard trajectory episode group identity is reused")
        groups.add(identity.episode_group_sha256)
        for key in (
            ("recording", identity.recording_hash),
            ("scenario_seed", f"{identity.scenario_manifest_sha256}:{identity.scenario_seed}"),
        ):
            if splits.setdefault(key, identity.split) != identity.split:
                raise ValueError("Onboard trajectory source identity crosses splits")
        for step in episode.steps:
            if step.frame_id in frames:
                raise ValueError("Onboard trajectory frame identity is reused across episodes")
            frames.add(step.frame_id)
        for start in range(0, len(episode.steps), stride):
            steps = episode.steps[start : start + window_size]
            padded = (*steps, *((None,) * (window_size - len(steps))))
            windows.append(SequenceWindow(episode, padded))
    return tuple(windows)
