from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest
from test_train_onboard_model import _trajectory_document, _write_trajectory_fixture

from multidetect.autonomy.contracts import ContextSnapshot
from multidetect.autonomy.onboard_agent import OnboardModelInput
from scripts import onboard_sequence_windows as sequences
from scripts import train_onboard_model as trainer


def _document(lengths: tuple[int, ...] = (5, 2, 1)) -> dict:
    """Extend the existing real-context trajectory fixture, not a new schema."""

    document = _trajectory_document()
    third = copy.deepcopy(document["episodes"][1])
    third.update(
        episode_id="episode-test",
        split="test",
        seed=13,
        scenario_seed=13,
        recording_id="episode-test-recording",
        recording_hash="6" * 64,
        task_contract_sha256="7" * 64,
    )
    third["episode_group_sha256"] = hashlib.sha256(
        "|".join(
            (
                third["recording_hash"],
                third["scenario_manifest_sha256"],
                third["airframe_profile_sha256"],
                str(third["scenario_seed"]),
                third["task_contract_sha256"],
            )
        ).encode()
    ).hexdigest()
    document["episodes"].append(third)
    offsets = (0.0, 0.25, 0.75, 1.5, 2.5)
    for episode, length in zip(document["episodes"], lengths, strict=True):
        template = episode["transitions"][0]
        steps = []
        for index in range(length):
            step = copy.deepcopy(template)
            step.update(
                sequence=index,
                transition_id=f"{episode['episode_id']}-transition-{index}",
                frame_id=f"{episode['episode_id']}-frame-{index}",
                next_frame_id=(
                    f"{episode['episode_id']}-frame-{index + 1}" if index + 1 < length else None
                ),
                observed_at_s=float(episode["seed"]) + offsets[index],
            )
            context_raw = step["observation"]["context"]
            context_raw.update(
                snapshot_id=f"{episode['episode_id']}-context-{index}",
                captured_at_s=step["observed_at_s"],
            )
            event_raw = step["observation"]["event"]
            event_raw.update(
                event_id=f"{episode['episode_id']}-event-{index}",
                observed_at_s=step["observed_at_s"],
            )
            context = ContextSnapshot.from_mapping(context_raw)
            event = trainer._onboard_event_from_mapping(event_raw)
            step["features"] = list(OnboardModelInput(context, event).feature_vector())
            source = step["source"]
            source.update(
                recording_id=episode["recording_id"],
                recording_hash=episode["recording_hash"],
                frame_id=step["frame_id"],
                captured_at_s=step["observed_at_s"],
                episode_group_sha256=episode["episode_group_sha256"],
                context_sha256=context.canonical_hash,
                event_sha256=event.canonical_hash,
                rgb_payload_sha256=hashlib.sha256(f"{step['frame_id']}:rgb".encode()).hexdigest(),
                depth_payload_sha256=hashlib.sha256(
                    f"{step['frame_id']}:depth".encode()
                ).hexdigest(),
            )
            # A single continuous mission can have several planner revisions.
            step["label_proof"]["executable_mission_sha256"] = hashlib.sha256(
                f"{step['frame_id']}:mission-revision".encode()
            ).hexdigest()
            steps.append(step)
        episode["transitions"] = steps
    return document


def _dataset(tmp_path: Path, *, document: dict | None = None) -> Path:
    return _write_trajectory_fixture(
        tmp_path, document=_document() if document is None else document
    )


def _load(tmp_path: Path) -> tuple[sequences.TrajectoryEpisode, ...]:
    return sequences.load_trajectory_episodes(_dataset(tmp_path), allow_unreviewed=True)


def test_existing_parser_and_sample_objects_are_reused(tmp_path: Path, monkeypatch) -> None:
    path = _dataset(tmp_path)
    original = trainer._trajectory_samples
    verified_rows = []

    def verified(*args, **kwargs):
        rows = original(*args, **kwargs)
        verified_rows.extend(rows)
        return rows

    monkeypatch.setattr(trainer, "_trajectory_samples", verified)
    episodes = sequences.load_trajectory_episodes(path, allow_unreviewed=True)
    steps = [step for episode in episodes for step in episode.steps]
    assert len(steps) == len(verified_rows) == 8
    assert all(step.sample is sample for step, sample in zip(steps, verified_rows, strict=True))
    assert all(isinstance(step.context, ContextSnapshot) for step in steps)
    assert [episode.identity.split for episode in episodes] == ["train", "evaluation", "test"]
    assert all(not episode.training_ready for episode in episodes)
    assert episodes[0].dataset_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


def test_windows_preserve_episode_order_dt_resets_and_unlabelled_padding(tmp_path: Path) -> None:
    episodes = _load(tmp_path)
    windows = sequences.collate_episode_windows(iter(episodes), window_size=3)
    assert [window.identity.episode_id for window in windows] == [
        "episode-train",
        "episode-train",
        "episode-evaluation",
        "episode-test",
    ]
    assert [
        [None if step is None else step.sequence for step in window.steps] for window in windows
    ] == [[0, 1, 2], [3, 4, None], [0, 1, None], [0, None, None]]
    assert windows[0].dt_s == (0.0, 0.25, 0.5)
    assert windows[1].dt_s == (0.75, 1.0, 0.0)
    assert windows[0].reset_mask == (True, False, False)
    assert windows[1].reset_mask == (False, False, False)
    assert windows[-1].reset_mask == (True, False, False)
    assert all(window.reset_before_window for window in windows)
    assert windows[-1].padding_mask == (False, True, True)
    assert windows[-1].samples == (episodes[-1].steps[0].sample, None, None)
    assert windows[-1].task_labels[1:] == (None, None)
    assert windows[-1].flight_labels[1:] == (None, None)
    assert all(
        step is None or step.identity == window.identity
        for window in windows
        for step in window.steps
    )
    assert len({step.executable_mission_sha256 for step in episodes[0].steps}) == 5


def test_overlapping_windows_retain_predecessor_dt_and_never_carry_state(tmp_path: Path) -> None:
    episodes = _load(tmp_path)
    windows = sequences.collate_episode_windows(episodes[:1], window_size=3, stride=2)
    assert [window.steps[0].sequence for window in windows] == [0, 2, 4]
    assert windows[1].dt_s == (0.5, 0.75, 1.0)
    assert windows[-1].dt_s == (1.0, 0.0, 0.0)
    assert windows[1].reset_mask == (False, False, False)
    assert all(window.reset_before_window for window in windows)


def test_immutable_views_cannot_be_mutated(tmp_path: Path) -> None:
    episode = _load(tmp_path)[0]
    window = sequences.collate_episode_windows([episode], window_size=2)[0]
    with pytest.raises(FrozenInstanceError):
        episode.training_ready = True
    with pytest.raises(FrozenInstanceError):
        episode.steps[0].dt_s = 100.0
    with pytest.raises(FrozenInstanceError):
        window.reset_before_window = False


@pytest.mark.parametrize("container", ["steps", "features", "regression"])
def test_in_memory_mutable_aliases_are_rejected(tmp_path: Path, container: str) -> None:
    episode = _load(tmp_path)[0]
    if container == "steps":
        episode = replace(episode, steps=list(episode.steps))
    else:
        sample = replace(
            episode.steps[0].sample,
            **{container: list(getattr(episode.steps[0].sample, container))},
        )
        first = replace(episode.steps[0], sample=sample)
        episode = replace(episode, steps=(first, *episode.steps[1:]))
    with pytest.raises(ValueError):
        sequences.collate_episode_windows([episode], window_size=2)


def test_default_rejects_unreviewed_even_when_labels_exist(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not training-ready"):
        sequences.load_trajectory_episodes(_dataset(tmp_path))


def test_training_ready_cannot_bypass_materialized_source_verification(tmp_path: Path) -> None:
    document = _document()
    document["training_ready"] = True
    with pytest.raises(ValueError, match="materialized_sources"):
        sequences.load_trajectory_episodes(_dataset(tmp_path, document=document))


def test_unreviewed_opt_in_never_returns_training_ready(tmp_path: Path, monkeypatch) -> None:
    path = _dataset(tmp_path)
    rows = trainer._trajectory_samples(path, allow_unreviewed=True)
    document = json.loads(path.read_text())
    document["training_ready"] = True
    path.write_text(json.dumps(document), encoding="utf-8")
    calls = []

    def parser_stub(dataset_path, *, allow_unreviewed=False):
        # Test only the output flag with existing verified typed rows.  This
        # stub is not evidence that the fixture has materialized Gazebo sources.
        calls.append((dataset_path, allow_unreviewed))
        return rows

    monkeypatch.setattr(trainer, "_trajectory_samples", parser_stub)
    episodes = sequences.load_trajectory_episodes(path, allow_unreviewed=True)
    assert calls == [(path.resolve(), True)]
    assert all(not episode.training_ready for episode in episodes)


@pytest.mark.parametrize("missing", ["task_label", "flight_label", "regression", "label_proof"])
def test_missing_labels_are_not_generated_from_planned_clip_conditions(
    tmp_path: Path, missing
) -> None:
    # The shared fixture writer needs label_proof only to bind its receipt.
    path = _dataset(tmp_path)
    valid = json.loads(path.read_text())
    valid["episodes"][0]["transitions"][0].pop(missing)
    valid["episodes"][0]["transitions"][0]["planned_condition"] = "clear"
    path.write_text(json.dumps(valid), encoding="utf-8")
    with pytest.raises(ValueError):
        sequences.load_trajectory_episodes(path, allow_unreviewed=True)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), True])
def test_invalid_numeric_transition_values_fail_closed(tmp_path: Path, value) -> None:
    document = _document()
    document["episodes"][0]["transitions"][0]["features"][0] = value
    with pytest.raises(ValueError):
        sequences.load_trajectory_episodes(
            _dataset(tmp_path, document=document), allow_unreviewed=True
        )


@pytest.mark.parametrize("replacement", ["NaN", "Infinity", "1e999"])
def test_nonfinite_unused_metadata_is_rejected(tmp_path: Path, replacement: str) -> None:
    path = _dataset(tmp_path)
    path.write_text(path.read_text()[:-1] + f', "unused": {replacement}}}', encoding="utf-8")
    with pytest.raises(ValueError, match="finite"):
        sequences.load_trajectory_episodes(path, allow_unreviewed=True)


@pytest.mark.parametrize("target", ["dataset", "receipt"])
def test_duplicate_json_keys_are_rejected(tmp_path: Path, target: str) -> None:
    path = _dataset(tmp_path)
    changed = path if target == "dataset" else tmp_path / "review-receipt.json"
    key = "schema" if target == "dataset" else "annotator"
    changed.write_text(f'{{"{key}": "duplicate",' + changed.read_text()[1:], encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON key"):
        sequences.load_trajectory_episodes(path, allow_unreviewed=True)


@pytest.mark.parametrize("field", ["sequence", "observed_at_s", "next_frame_id", "recording_hash"])
def test_raw_episode_order_and_source_identity_are_still_verified(
    tmp_path: Path, field: str
) -> None:
    document = _document()
    steps = document["episodes"][0]["transitions"]
    if field == "sequence":
        steps[0], steps[1] = steps[1], steps[0]
    elif field == "observed_at_s":
        steps[1][field] = steps[0][field]
    elif field == "next_frame_id":
        steps[0][field] = document["episodes"][1]["transitions"][0]["frame_id"]
    else:
        steps[0]["source"][field] = document["episodes"][1]["recording_hash"]
    with pytest.raises(ValueError):
        sequences.load_trajectory_episodes(
            _dataset(tmp_path, document=document), allow_unreviewed=True
        )


@pytest.mark.parametrize("target", ["dataset", "receipt"])
def test_input_change_during_parser_call_is_rejected(
    tmp_path: Path, monkeypatch, target: str
) -> None:
    path = _dataset(tmp_path)
    changed = path if target == "dataset" else tmp_path / "review-receipt.json"
    original = trainer._trajectory_samples

    def mutate(*args, **kwargs):
        rows = original(*args, **kwargs)
        changed.write_text(changed.read_text() + "\n", encoding="utf-8")
        return rows

    monkeypatch.setattr(trainer, "_trajectory_samples", mutate)
    with pytest.raises(ValueError, match="changed during validation"):
        sequences.load_trajectory_episodes(path, allow_unreviewed=True)


@pytest.mark.parametrize("mutation", ["order", "dt", "reset", "nonfinite", "mission", "sample"])
def test_in_memory_identity_and_temporal_mixing_is_rejected(tmp_path: Path, mutation: str) -> None:
    episode, other, _ = _load(tmp_path)
    steps = list(episode.steps)
    if mutation == "order":
        steps[0], steps[1] = steps[1], steps[0]
    elif mutation == "dt":
        steps[1] = replace(steps[1], dt_s=0.0)
    elif mutation == "reset":
        steps[1] = replace(steps[1], episode_reset=True)
    elif mutation == "nonfinite":
        steps[1] = replace(steps[1], observed_at_s=float("nan"))
    elif mutation == "mission":
        steps[1] = replace(steps[1], identity=other.identity)
    else:
        steps[1] = replace(steps[1], sample=other.steps[0].sample)
    with pytest.raises(ValueError):
        sequences.collate_episode_windows([replace(episode, steps=tuple(steps))], window_size=2)


def test_task_contract_cannot_be_relabelled_without_its_bound_group(tmp_path: Path) -> None:
    episode = _load(tmp_path)[0]
    identity = replace(episode.identity, task_contract_sha256="b" * 64)
    with pytest.raises(ValueError, match="mission/episode group identity"):
        sequences.collate_episode_windows([replace(episode, identity=identity)], window_size=2)


def test_duplicate_episode_groups_cannot_be_recollated_as_new_episodes(tmp_path: Path) -> None:
    episode = _load(tmp_path)[0]
    with pytest.raises(ValueError, match="group identity is reused"):
        sequences.collate_episode_windows([episode, episode], window_size=2)


@pytest.mark.parametrize("window_size,stride", [(0, None), (True, None), (2, 0), (2, True), (2, 3)])
def test_invalid_window_configuration_is_rejected(tmp_path: Path, window_size, stride) -> None:
    with pytest.raises(ValueError):
        sequences.collate_episode_windows(_load(tmp_path), window_size=window_size, stride=stride)


def test_empty_episode_input_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one episode"):
        sequences.collate_episode_windows([], window_size=2)
