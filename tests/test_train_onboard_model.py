from __future__ import annotations

import copy
import hashlib
import json
import math
import random
from pathlib import Path

import pytest

from multidetect.autonomy import (
    ONBOARD_MODEL_ACTION_ORDER,
    ONBOARD_MODEL_FEATURE_NAMES,
    ONBOARD_MODEL_TASK_ACTION_ORDER,
    ONBOARD_MODEL_TASK_PHASES,
    ContextSnapshot,
    GeoPoint,
    OnboardEventKind,
    OnboardFlightAction,
    OnboardModelInput,
    OnboardRuntimeEvent,
    OnboardTaskAction,
)
from scripts.train_onboard_model import (
    NAVIGATION_SIGN_MARGIN_CAP,
    NAVIGATION_SIGN_MARGIN_FLOOR,
    BootstrapSample,
    _bearing_anchor_samples,
    _epoch_sample_indices,
    _gazebo_3d_geometry_samples,
    _gazebo_3d_transition_samples,
    _navigation_geometry_loss,
    _navigation_sign_margin_loss,
    _neutral_target_transition_samples,
    _parent_distillation_loss,
    _partition_bootstrap_samples,
    _phase_transition_samples,
    _require_real_3d_capture,
    _samples,
    _semantic_transition_samples,
    _structural_scene_geometry,
    _structural_target_geometry,
    _teacher,
    _temporal_transition_samples,
    _trajectory_samples,
)
from scripts.train_onboard_model import main as train_onboard_main

ROOT = Path(__file__).resolve().parents[1]


def test_epoch_shuffle_is_complete_reproducible_and_changes_per_epoch() -> None:
    pytest.importorskip("torch")
    first = _epoch_sample_indices(256, 20261007, 0).tolist()
    assert sorted(first) == list(range(256))
    assert first == _epoch_sample_indices(256, 20261007, 0).tolist()
    assert first != _epoch_sample_indices(256, 20261007, 1).tolist()
    assert first != list(range(256))


def test_action_distillation_does_not_restore_bad_parent_geometry() -> None:
    torch = pytest.importorskip("torch")
    output = torch.zeros((2, 20), requires_grad=True)
    parent = torch.zeros((2, 20))
    parent[:, -4:] = 10.0
    action_loss = _parent_distillation_loss(output, parent, "actions")
    action_loss.backward()
    assert float(action_loss) == 0.0
    assert torch.equal(output.grad[:, -4:], torch.zeros((2, 4)))
    assert float(_parent_distillation_loss(output, parent, "all")) > 0.0
    with pytest.raises(ValueError, match="scope"):
        _parent_distillation_loss(output, parent, "invalid")


def test_real_capture_mode_forbids_synthetic_bootstrap() -> None:
    with pytest.raises(ValueError, match="requires --no-synthetic-bootstrap"):
        train_onboard_main(
            [
                "--output-directory",
                "candidate",
                "--require-real-3d-capture",
            ]
        )


def test_no_synthetic_bootstrap_cannot_create_an_empty_corpus() -> None:
    with pytest.raises(ValueError, match="training corpus is empty"):
        _samples(300, 20261007, include_synthetic_bootstrap=False)


def test_navigation_sign_margin_loss_is_zero_at_target_magnitude() -> None:
    torch = pytest.importorskip("torch")
    targets = torch.tensor([[0.20, -0.40], [0.0, 0.0]], dtype=torch.float32)
    predictions = targets.clone()
    loss = _navigation_sign_margin_loss(predictions, targets)
    assert float(loss) == pytest.approx(0.0)


def test_navigation_sign_margin_loss_does_not_reward_excessive_magnitude() -> None:
    torch = pytest.importorskip("torch")
    target = torch.tensor([[0.20, -0.40]], dtype=torch.float32)
    exact = _navigation_sign_margin_loss(target, target)
    excessive = _navigation_sign_margin_loss(
        torch.tensor([[3.0, -3.0]], dtype=torch.float32),
        target,
    )
    assert float(exact) == pytest.approx(0.0)
    assert float(excessive) == pytest.approx(0.0)


def test_navigation_sign_margin_loss_corrects_wrong_sign_with_finite_margin() -> None:
    torch = pytest.importorskip("torch")
    targets = torch.tensor([[0.20, -0.40]], dtype=torch.float32)
    wrong = torch.tensor([[-0.20, 0.40]], dtype=torch.float32, requires_grad=True)
    loss = _navigation_sign_margin_loss(wrong, targets)
    loss.backward()
    assert float(loss) > 0.0
    assert wrong.grad is not None
    assert float(wrong.grad[0, 0]) < 0.0
    assert float(wrong.grad[0, 1]) > 0.0
    assert NAVIGATION_SIGN_MARGIN_FLOOR > 0.0
    assert NAVIGATION_SIGN_MARGIN_CAP >= NAVIGATION_SIGN_MARGIN_FLOOR


def test_navigation_geometry_loss_uses_observed_bearing_and_range() -> None:
    torch = pytest.importorskip("torch")
    bearing = -1.0 / 3.0
    distance_fraction = 75.0 / 5_000.0
    features = torch.tensor(
        [[0.0] * 14 + [bearing, distance_fraction] + [0.0] * 24],
        dtype=torch.float32,
    )
    follow_label = ONBOARD_MODEL_ACTION_ORDER.index(OnboardFlightAction.FOLLOW_TARGET)
    targets = torch.tensor([follow_label], dtype=torch.long)
    expected = torch.tensor(
        [[math.cos(bearing * math.pi) * 0.15, math.sin(bearing * math.pi) * 0.15]],
        dtype=torch.float32,
    )
    assert float(_navigation_geometry_loss(expected, features, targets)) == pytest.approx(
        0.0, abs=2.0e-6
    )


def _trajectory_document(*, training_ready: bool = False) -> dict:
    def episode(episode_id: str, split: str, seed: int) -> dict:
        # The fixture uses the real ContextSnapshot -> OnboardModelInput path,
        # rather than hand-written zero vectors.  This is the same feature
        # recomputation required for reviewed Gazebo episodes.
        context = ContextSnapshot(
            snapshot_id=episode_id,
            captured_at_s=float(seed),
            captured_at_utc="2026-10-05T00:00:00Z",
            aircraft_position=GeoPoint(1.3, 103.8),
            home_position=GeoPoint(1.3, 103.8),
            airframe_profile_id="px4_vtol_gazebo_v1",
            battery_remaining_pct=80.0,
            estimated_flight_time_s=900.0,
            ground_speed_mps=0.0,
            airspeed_mps=0.0,
            wind_north_mps=0.0,
            wind_east_mps=0.0,
            gps_fix_type=3,
            gps_horizontal_accuracy_m=1.0,
            geofence_valid=True,
            camera_available=True,
            perception_available=True,
            map_cache_available=True,
            dem_available=True,
        )
        event = OnboardRuntimeEvent(
            event_id=f"{episode_id}-event",
            kind=OnboardEventKind.TARGET_LOST,
            observed_at_s=float(seed),
            severity=0,
            detail="target_lost",
            task_phase=OnboardTaskAction.SEARCH,
        )
        recording_hash = ("9" if split == "train" else "a") * 64
        rgb_payload_hash = ("1" if split == "train" else "b") * 64
        scenario_manifest_hash = "5" * 64
        airframe_hash = "8" * 64
        task_contract_hash = "d" * 64
        scenario_seed = seed
        episode_group_hash = hashlib.sha256(
            "|".join(
                (
                    recording_hash,
                    scenario_manifest_hash,
                    airframe_hash,
                    str(scenario_seed),
                    task_contract_hash,
                )
            ).encode("utf-8")
        ).hexdigest()
        source = {
            "recording_id": f"{episode_id}-recording",
            "recording_hash": recording_hash,
            "frame_id": f"{episode_id}-frame-0",
            "captured_at_s": float(seed),
            "episode_group_sha256": episode_group_hash,
            "rgb_payload_sha256": rgb_payload_hash,
            "depth_payload_sha256": ("2" if split == "train" else "c") * 64,
            "rgb_manifest_sha256": "3" * 64,
            "depth_manifest_sha256": "4" * 64,
            "scenario_manifest_sha256": scenario_manifest_hash,
            "context_sha256": context.canonical_hash,
            "event_sha256": event.canonical_hash,
        }
        return {
            "episode_id": episode_id,
            "scene_id": "river_rescue_vtol_001",
            "scenario_manifest_sha256": scenario_manifest_hash,
            "airframe_profile_id": context.airframe_profile_id,
            "airframe_profile_sha256": airframe_hash,
            "recording_id": source["recording_id"],
            "recording_hash": recording_hash,
            "recording_bound": True,
            "scenario_seed": scenario_seed,
            "task_contract_sha256": task_contract_hash,
            "episode_group_sha256": episode_group_hash,
            "seed": seed,
            "split": split,
            "outcome": "completed",
            "transitions": [
                {
                    "sequence": 0,
                    "transition_id": f"{episode_id}-transition-0",
                    "frame_id": source["frame_id"],
                    "next_frame_id": None,
                    "observed_at_s": float(seed),
                    "observation": {
                        "context": context.to_mapping(),
                        "event": event.to_mapping(),
                    },
                    "features": list(OnboardModelInput(context, event).feature_vector()),
                    "task_label": ONBOARD_MODEL_TASK_ACTION_ORDER.index(OnboardTaskAction.SEARCH),
                    "flight_label": ONBOARD_MODEL_ACTION_ORDER.index(
                        OnboardFlightAction.MOVE_RELATIVE
                    ),
                    "regression": [0.0] * 4,
                    "event_kind": event.kind.value,
                    "task_phase": event.task_phase.value,
                    "source": copy.deepcopy(source),
                    "label_proof": {
                        "review_receipt_sha256": "0" * 64,
                        "annotation_policy_sha256": "e" * 64,
                        "decision_sha256": "f" * 64,
                        "plan_sha256": "1" * 64,
                        "wire_hash": "2" * 64,
                        "executable_mission_sha256": "3" * 64,
                    },
                    "planner_revalidated": True,
                    "truth_free": True,
                    "policy_applied_to_actuator": False,
                }
            ],
        }

    return {
        "schema": "multidetect.onboard-trajectory-dataset",
        "schema_version": 1,
        "contract": "multidetect.onboard-trajectory-dataset-v1",
        "feature_names": list(ONBOARD_MODEL_FEATURE_NAMES),
        "task_action_order": [item.value for item in ONBOARD_MODEL_TASK_ACTION_ORDER],
        "flight_action_order": [item.value for item in ONBOARD_MODEL_ACTION_ORDER],
        "label_source": "planner_revalidated",
        "feature_provenance": "recomputed_from_context_event_v1",
        "training_ready": training_ready,
        "truth_free": True,
        "truth_injected": False,
        "policy_applied_to_actuator": False,
        "review_receipt": {
            "path": "review-receipt.json",
            "sha256": "0" * 64,
            "replay_manifest_sha256": "4" * 64,
            "annotator": "annotator",
            "reviewer": "reviewer",
            "independent_review_recorded": True,
        },
        "split_strategy": "episode_scene_airframe_seed",
        "source_manifests": [
            {
                "path": "rgb-manifest.json",
                "sha256": "3" * 64,
                "scenario_manifest_sha256": "5" * 64,
                "pixel_source": "actual_gazebo_camera_sensor_output",
            }
        ],
        "episodes": [
            episode("episode-train", "train", 11),
            episode("episode-evaluation", "evaluation", 12),
        ],
    }


def _write_trajectory_fixture(
    tmp_path: Path, *, training_ready: bool = False, document: dict | None = None
) -> Path:
    document = _trajectory_document(training_ready=training_ready) if document is None else document
    receipt_path = tmp_path / "review-receipt.json"
    receipt_path.write_text(
        json.dumps(
            {
                "replay_manifest_sha256": "4" * 64,
                "annotation_policy_sha256": "e" * 64,
                "annotator": "annotator",
                "reviewer": "reviewer",
                "independent_review_recorded": True,
            }
        ),
        encoding="utf-8",
    )
    receipt_hash = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    document["review_receipt"]["sha256"] = receipt_hash
    for episode in document["episodes"]:
        for transition in episode["transitions"]:
            transition["label_proof"]["review_receipt_sha256"] = receipt_hash
    path = tmp_path / "trajectory.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_no_synthetic_bootstrap_keeps_only_trajectory_rows(tmp_path: Path) -> None:
    path = _write_trajectory_fixture(tmp_path)

    rows = _samples(
        300,
        20261007,
        include_synthetic_bootstrap=False,
        trajectory_dataset=path,
        allow_unreviewed_trajectory=True,
    )

    assert len(rows) == 2
    assert all(row.family.startswith("trajectory_") for row in rows)


def test_reviewed_trajectory_loader_preserves_episode_groups(tmp_path: Path) -> None:
    path = _write_trajectory_fixture(tmp_path)

    rows = _trajectory_samples(path, allow_unreviewed=True)

    assert len(rows) == 2
    assert all(row.group_id.startswith("trajectory:") for row in rows)
    assert len({row.group_id for row in rows}) == 2
    assert {row.split for row in rows} == {"train", "evaluation"}
    assert all(row.family.startswith("trajectory_river_rescue_vtol_001") for row in rows)


def test_trajectory_loader_requires_reviewed_ready_contract(tmp_path: Path) -> None:
    path = _write_trajectory_fixture(tmp_path, training_ready=False)

    with pytest.raises(ValueError, match="not training-ready"):
        _trajectory_samples(path)

    assert len(_trajectory_samples(path, allow_unreviewed=True)) == 2


def test_real_3d_capture_gate_requires_both_materialized_sources() -> None:
    with pytest.raises(ValueError, match="requires both"):
        _require_real_3d_capture(None, None)


def test_real_3d_capture_gate_rejects_unbound_trajectory_episode(tmp_path: Path) -> None:
    rendered_path = tmp_path / "rendered.json"
    rendered_path.write_text(
        json.dumps(
            {
                "rows": [],
                "training_ready": True,
                "external_gazebo_runtime": True,
                "pixel_level_rendered": True,
                "depth_required": True,
                "truth_free": True,
                "truth_injected": False,
            }
        ),
        encoding="utf-8",
    )
    trajectory_path = tmp_path / "trajectory.json"
    trajectory_path.write_text(
        json.dumps(
            {
                "episodes": [
                    {
                        "scene_id": "urban_inspection_multirotor_001",
                        "recording_bound": False,
                        "transitions": [{"transition_id": "t0"}],
                    }
                ],
                "training_ready": True,
                "materialized_sources": True,
                "truth_free": True,
                "truth_injected": False,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="not every trajectory episode is recording-bound"):
        _require_real_3d_capture(
            rendered_path,
            trajectory_path,
            minimum_rows=0,
            minimum_scenes=0,
        )


def test_real_3d_capture_gate_requires_calibrated_ned_geometry(tmp_path: Path) -> None:
    rendered_path = tmp_path / "rendered.json"
    rendered_path.write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "source": {
                            "frame_id": f"frame-{index}",
                            "scenario_id": "urban_inspection_multirotor_001",
                            "depth_median": 30.0,
                            "detector_count": 1,
                            "geometry_source": "missing_camera_calibration",
                        }
                    }
                    for index in range(2)
                ],
                "training_ready": True,
                "external_gazebo_runtime": True,
                "pixel_level_rendered": True,
                "depth_required": True,
                "truth_free": True,
                "truth_injected": False,
            }
        ),
        encoding="utf-8",
    )
    trajectory_path = tmp_path / "trajectory.json"
    trajectory_path.write_text(
        json.dumps(
            {
                "episodes": [
                    {
                        "scene_id": "urban_inspection_multirotor_001",
                        "recording_bound": True,
                        "transitions": [{"transition_id": "t0"}],
                    }
                ],
                "training_ready": True,
                "materialized_sources": True,
                "truth_free": True,
                "truth_injected": False,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="calibrated NED geometry rows"):
        _require_real_3d_capture(
            rendered_path,
            trajectory_path,
            minimum_rows=2,
            minimum_scenes=1,
        )


def test_real_3d_capture_gate_requires_independent_test_episode(tmp_path: Path) -> None:
    rendered_path = tmp_path / "rendered.json"
    rendered_path.write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "source": {
                            "frame_id": f"frame-{index}",
                            "scenario_id": "urban_inspection_multirotor_001",
                            "depth_median": 30.0,
                            "detector_count": 1,
                            "geometry_source": "camera_calibration_depth_pose_ned",
                        }
                    }
                    for index in range(2)
                ],
                "training_ready": True,
                "external_gazebo_runtime": True,
                "pixel_level_rendered": True,
                "depth_required": True,
                "truth_free": True,
                "truth_injected": False,
            }
        ),
        encoding="utf-8",
    )
    trajectory_path = tmp_path / "trajectory.json"
    trajectory_path.write_text(
        json.dumps(
            {
                "episodes": [
                    {
                        "scene_id": "urban_inspection_multirotor_001",
                        "split": "train",
                        "recording_bound": True,
                        "transitions": [{"transition_id": "t0"}],
                    },
                    {
                        "scene_id": "river_rescue_vtol_001",
                        "split": "evaluation",
                        "recording_bound": True,
                        "transitions": [{"transition_id": "e0"}],
                    },
                ],
                "training_ready": True,
                "materialized_sources": True,
                "truth_free": True,
                "truth_injected": False,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="independent trajectory test transitions"):
        _require_real_3d_capture(
            rendered_path,
            trajectory_path,
            minimum_rows=0,
            minimum_scenes=1,
        )


def test_real_3d_capture_gate_requires_temporal_and_health_events(tmp_path: Path) -> None:
    rendered_path = tmp_path / "rendered.json"
    rendered_path.write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "source": {
                            "frame_id": "frame-0",
                            "scenario_id": "urban_inspection_multirotor_001",
                            "depth_median": 30.0,
                            "detector_count": 1,
                            "geometry_source": "camera_calibration_depth_pose_ned",
                        }
                    }
                ],
                "training_ready": True,
                "external_gazebo_runtime": True,
                "pixel_level_rendered": True,
                "depth_required": True,
                "truth_free": True,
                "truth_injected": False,
            }
        ),
        encoding="utf-8",
    )
    trajectory_path = tmp_path / "trajectory.json"
    trajectory_path.write_text(
        json.dumps(
            {
                "episodes": [
                    {
                        "scene_id": "urban_inspection_multirotor_001",
                        "split": "train",
                        "recording_bound": True,
                        "transitions": [{"transition_id": "t0", "event_kind": "target_lost"}],
                    },
                    {
                        "scene_id": "river_rescue_vtol_001",
                        "split": "test",
                        "recording_bound": True,
                        "transitions": [{"transition_id": "t1", "event_kind": "target_lost"}],
                    },
                ],
                "training_ready": True,
                "materialized_sources": True,
                "truth_free": True,
                "truth_injected": False,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="missing required temporal event kinds"):
        _require_real_3d_capture(
            rendered_path,
            trajectory_path,
            minimum_rows=0,
            minimum_scenes=1,
        )


def test_real_3d_capture_gate_rejects_colour_fixture_scenes(tmp_path: Path) -> None:
    rendered_path = tmp_path / "rendered.json"
    rendered_path.write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "source": {
                            "frame_id": "frame-0",
                            "scenario_id": "gazebo_red_green_search_001",
                            "depth_median": 30.0,
                            "detector_count": 1,
                            "geometry_source": "camera_calibration_depth_pose_ned",
                        }
                    }
                ],
                "training_ready": True,
                "external_gazebo_runtime": True,
                "pixel_level_rendered": True,
                "depth_required": True,
                "truth_free": True,
                "truth_injected": False,
            }
        ),
        encoding="utf-8",
    )
    trajectory_path = tmp_path / "trajectory.json"
    trajectory_path.write_text(
        json.dumps(
            {
                "episodes": [],
                "training_ready": True,
                "materialized_sources": True,
                "truth_free": True,
                "truth_injected": False,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="excluded colour-fixture scenes"):
        _require_real_3d_capture(
            rendered_path,
            trajectory_path,
            minimum_rows=0,
            minimum_scenes=0,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda document: document["episodes"][0]["transitions"][0].update(
            planner_revalidated=False
        ),
        lambda document: document["episodes"][0]["transitions"][0]["source"].update(
            depth_payload_sha256="bad"
        ),
        lambda document: document["episodes"].append(copy.deepcopy(document["episodes"][0])),
    ],
)
def test_trajectory_loader_fails_closed_on_provenance_or_group_reuse(tmp_path, mutation) -> None:
    document = _trajectory_document()
    mutation(document)
    path = _write_trajectory_fixture(tmp_path, document=document)

    with pytest.raises(ValueError):
        _trajectory_samples(path)


def test_trajectory_rows_can_be_added_to_the_existing_training_corpus(tmp_path: Path) -> None:
    path = _write_trajectory_fixture(tmp_path)

    rows = _samples(
        240,
        20261005,
        trajectory_dataset=path,
        allow_unreviewed_trajectory=True,
    )

    assert any(row.family.startswith("trajectory_") for row in rows)
    assert all(len(row.features) == len(ONBOARD_MODEL_FEATURE_NAMES) for row in rows)


def test_materialized_trajectory_sources_fail_closed_without_real_manifests(tmp_path: Path) -> None:
    document = _trajectory_document()
    document["materialized_sources"] = True
    document["training_ready"] = True
    path = _write_trajectory_fixture(tmp_path, document=document)

    with pytest.raises(ValueError, match="RGB manifest file is missing"):
        _trajectory_samples(path)


def test_training_ready_trajectory_requires_materialized_sources(tmp_path: Path) -> None:
    document = _trajectory_document(training_ready=True)
    path = _write_trajectory_fixture(tmp_path, document=document)

    with pytest.raises(ValueError, match="materialized_sources=true"):
        _trajectory_samples(path)


def test_declared_episode_test_split_never_enters_train_or_evaluation() -> None:
    def row(group: str, split: str) -> BootstrapSample:
        return BootstrapSample((0.0,) * 40, 0, 0, (0.0,) * 4, group, group, split)

    train_rows, eval_rows, test_rows, strategy = _partition_bootstrap_samples(
        [
            row("episode-train", "train"),
            row("episode-evaluation", "evaluation"),
            row("episode-test", "test"),
        ],
        20261005,
    )

    assert strategy == "declared_episode_split_plus_source_group_hash"
    assert {item.group_id for item in train_rows} == {"episode-train"}
    assert {item.group_id for item in eval_rows} == {"episode-evaluation"}
    assert {item.group_id for item in test_rows} == {"episode-test"}


def test_declared_episode_group_split_conflict_fails_closed() -> None:
    rows = [
        BootstrapSample((0.0,) * 40, 0, 0, (0.0,) * 4, "episode", "episode", "train"),
        BootstrapSample((0.0,) * 40, 0, 0, (0.0,) * 4, "episode", "episode", "test"),
    ]

    with pytest.raises(ValueError, match="crosses declared splits"):
        _partition_bootstrap_samples(rows, 20261005)


def test_new_training_defaults_are_semantic_not_red_car_fixture() -> None:
    rows = _samples(
        240,
        20261005,
        temporal_augment=True,
        semantic_augment=True,
        phase_augment=True,
    )
    families = {row.family for row in rows}
    assert not any(family.startswith("red_car_") for family in families)
    assert any(family.startswith("semantic_") for family in families)
    assert "phase_takeoff_target_present" in families


def test_semantic_samples_cover_unknown_and_attribute_mismatch_states() -> None:
    rows = _semantic_transition_samples(120, 20261005)
    families = {row.family for row in rows}
    assert any("class_color_mismatch" in family for family in families)
    assert any("class_only_mismatch" in family for family in families)
    assert any("color_only_mismatch" in family for family in families)
    assert any("attributes_unknown" in family for family in families)
    assert any("unknown_abstain" in family for family in families)


def test_neutral_target_samples_allow_class_match_without_colour_constraint() -> None:
    rows = _neutral_target_transition_samples(40, 20261006)
    assert rows
    assert all(len(row.features) == len(ONBOARD_MODEL_FEATURE_NAMES) for row in rows)
    assert any(row.family == "neutral_target_match_follow" for row in rows)
    assert any(row.family == "neutral_target_record_return" for row in rows)
    color_index = ONBOARD_MODEL_FEATURE_NAMES.index("target_color_match")
    class_index = ONBOARD_MODEL_FEATURE_NAMES.index("target_class_match")
    assert all(row.features[class_index] == 1.0 for row in rows)
    assert all(row.features[color_index] == 0.5 for row in rows)


def test_neutral_target_teacher_keeps_range_bearing_and_navigation_consistent() -> None:
    rows = _neutral_target_transition_samples(80, 20261006)
    for row in rows:
        distance_m = row.features[15] * 5_000.0
        bearing = row.features[14]
        north = math.tanh(row.regression[0])
        east = math.tanh(row.regression[1])
        if row.family == "neutral_target_record_return":
            assert north == pytest.approx(0.0)
            assert east == pytest.approx(0.0)
            continue
        assert distance_m == pytest.approx(math.hypot(north, east) * 500.0)
        assert north == pytest.approx(math.cos(bearing * math.pi) * distance_m / 500.0)
        assert east == pytest.approx(math.sin(bearing * math.pi) * distance_m / 500.0)


def test_teacher_does_not_label_unknown_attributes_as_a_match() -> None:
    rows = [_teacher(random.Random(seed)) for seed in range(500)]
    for row in rows:
        target_match = row.features[17]
        class_match = row.features[18]
        color_match = row.features[19]
        if class_match < 0.99 or color_match < 0.99:
            assert target_match < 0.8


def test_teacher_obstacle_distance_uses_metre_contract() -> None:
    rows = [_teacher(random.Random(seed)) for seed in range(2_000)]
    obstacle_rows = [row for row in rows if row.family == "obstacle_hold"]
    assert obstacle_rows
    assert all(row.features[21] * 5_000.0 < 0.10 for row in obstacle_rows)


def test_gazebo_3d_augmentation_uses_all_mainline_vehicle_profiles() -> None:
    rows = _gazebo_3d_transition_samples(
        90,
        20261005,
        ROOT / "configs/scenarios/gazebo-3d-mainline-v1.manifest.json",
    )
    families = {row.family for row in rows}
    assert any("multirotor" in family for family in families)
    assert any("vtol" in family for family in families)
    assert any("fixed-wing" in family for family in families)
    assert all("red_car" not in family and "green" not in family for family in families)
    assert all(len(row.features) == 40 for row in rows)


def test_gazebo_3d_teacher_covers_cartesian_scene_event_phase_contract() -> None:
    """Do not let one index silently couple scene, fault and task phase.

    This is a training-data invariant, not a Gazebo runtime claim.  Every
    approved vehicle scene must expose every runtime event in every one of the
    nine shared task phases, with five independent semantic states.  The model
    therefore sees the same lifecycle/safety combinations that the live
    Jetson adapter can receive instead of learning an index-specific shortcut.
    """

    rows = _gazebo_3d_transition_samples(
        3 * len(OnboardEventKind) * len(ONBOARD_MODEL_TASK_PHASES) * 5,
        20261006,
        ROOT / "configs/scenarios/gazebo-3d-mainline-v1.manifest.json",
    )
    grouped: dict[tuple[str, int, int], list[object]] = {}
    for row in rows:
        prefix, _, remainder = row.group_id.partition("-event-")
        scenario_id = prefix.removeprefix("gazebo-3d-")
        event_text, _, _semantic = remainder.partition("-phase-")
        event_index = int(event_text)
        phase_start = remainder.index("-phase-") + len("-phase-")
        phase_index = int(remainder[phase_start:].split("-semantic-", 1)[0])
        grouped.setdefault((scenario_id, event_index, phase_index), []).append(row)

    assert len(grouped) == 3 * len(OnboardEventKind) * len(ONBOARD_MODEL_TASK_PHASES)
    assert all(len(group) == 5 for group in grouped.values())
    assert {key[0] for key in grouped} == {
        "urban_inspection_multirotor_001",
        "river_rescue_vtol_001",
        "coastal_patrol_fixed_wing_001",
    }
    assert {key[1] for key in grouped} == set(range(len(OnboardEventKind)))
    assert {key[2] for key in grouped} == set(range(len(ONBOARD_MODEL_TASK_PHASES)))


def test_gazebo_3d_teacher_keeps_illustrative_colour_unknown() -> None:
    rows = _gazebo_3d_transition_samples(
        300,
        20261006,
        ROOT / "configs/scenarios/gazebo-3d-mainline-v1.manifest.json",
    )
    # Feature 19 is retained for backwards-compatible OnboardAI artifacts,
    # but a neutral 3-D scene must not teach the policy that one colour is the
    # target identity.  Identity comes from class/tracker agreement and the
    # generic target-match score (features 17/18).
    assert rows
    assert all(row.features[19] == pytest.approx(0.5) for row in rows)
    assert any(
        row.features[17] >= 0.8
        and row.features[18] >= 0.99
        and row.task_label
        in {
            ONBOARD_MODEL_TASK_ACTION_ORDER.index(OnboardTaskAction.MATCH_TARGET),
            ONBOARD_MODEL_TASK_ACTION_ORDER.index(OnboardTaskAction.RECORD_LOCATION),
        }
        for row in rows
    )


def test_gazebo_teacher_reads_entity_sdf_geometry_without_colour_or_truth() -> None:
    geometry = _structural_target_geometry(
        ROOT / "configs/scenarios/gazebo-3d-mainline-v1.manifest.json"
    )

    assert set(geometry) == {
        "urban_inspection_multirotor_001",
        "river_rescue_vtol_001",
        "coastal_patrol_fixed_wing_001",
    }
    assert all(item["primitive_count"] >= 10.0 for item in geometry.values())
    assert all(item["radial_extent_m"] > 1.0 for item in geometry.values())
    assert all(item["height_m"] > 2.0 for item in geometry.values())
    model_text = (
        ROOT / "simulation/gazebo/models/multidetect_search_target_v1/model.sdf"
    ).read_text(encoding="utf-8").lower()
    assert "red" not in model_text
    assert "green" not in model_text
    assert "ground_truth" not in model_text


def test_gazebo_teacher_reads_world_geometry_without_using_fixed_target_labels() -> None:
    geometry = _structural_scene_geometry(
        ROOT / "configs/scenarios/gazebo-3d-mainline-v1.manifest.json"
    )
    assert set(geometry) == {
        "urban_inspection_multirotor_001",
        "river_rescue_vtol_001",
        "coastal_patrol_fixed_wing_001",
    }
    assert all(item["primitive_count"] > 0.0 for item in geometry.values())
    assert all(item["collision_primitive_count"] > 0.0 for item in geometry.values())
    assert all(0.0 < item["complexity_fraction"] <= 1.0 for item in geometry.values())
    world_text = "\n".join(
        (
            ROOT / "simulation/gazebo/worlds" / f"{scenario}.sdf"
        ).read_text(encoding="utf-8").lower()
        for scenario in (
            "urban_inspection_multirotor_001",
            "river_rescue_vtol_001",
            "coastal_patrol_fixed_wing_001",
        )
    )
    assert "red_car" not in world_text
    assert "green_car" not in world_text


def test_gazebo_3d_augmentation_can_be_added_without_changing_feature_contract() -> None:
    rows = _samples(
        240,
        20261005,
        gazebo_3d_manifest=ROOT / "configs/scenarios/gazebo-3d-mainline-v1.manifest.json",
    )
    assert any(row.family.startswith("gazebo_3d_") for row in rows)
    assert all(len(row.features) == 40 for row in rows)


def test_gazebo_3d_geometry_curriculum_binds_follow_to_ned_bearing_and_range() -> None:
    rows = _gazebo_3d_geometry_samples(
        180,
        20261006,
        ROOT / "configs/scenarios/gazebo-3d-mainline-v1.manifest.json",
    )
    follow_label = ONBOARD_MODEL_ACTION_ORDER.index(OnboardFlightAction.FOLLOW_TARGET)
    match_label = ONBOARD_MODEL_TASK_ACTION_ORDER.index(OnboardTaskAction.MATCH_TARGET)
    assert len(rows) == 180
    assert all(row.flight_label == follow_label for row in rows)
    assert all(row.task_label == match_label for row in rows)
    assert all(row.features[19] == pytest.approx(0.5) for row in rows)
    for row in rows:
        distance_m = row.features[15] * 5_000.0
        north_m = math.tanh(row.regression[0]) * 500.0
        east_m = math.tanh(row.regression[1]) * 500.0
        assert math.hypot(north_m, east_m) == pytest.approx(distance_m, rel=1.0e-6)
        assert row.features[14] == pytest.approx(
            math.atan2(east_m, north_m) / math.pi,
            abs=1.0e-6,
        )


def test_gazebo_3d_curriculum_covers_independent_scene_event_phase_states() -> None:
    rows = _gazebo_3d_transition_samples(
        3 * len(OnboardEventKind) * len(ONBOARD_MODEL_TASK_PHASES) * 5,
        20261010,
        ROOT / "configs/scenarios/gazebo-3d-mainline-v1.manifest.json",
    )
    scenario_ids = (
        "urban_inspection_multirotor_001",
        "river_rescue_vtol_001",
        "coastal_patrol_fixed_wing_001",
    )
    combinations: set[tuple[str, int, int]] = set()
    semantic_states: dict[tuple[str, int, int], set[int]] = {}
    for row in rows:
        scenario_id = next(
            candidate
            for candidate in scenario_ids
            if row.family.startswith(f"gazebo_3d_{candidate}_")
        )
        event_index = row.features[24:31].index(1.0)
        phase_index = row.features[31:40].index(1.0)
        base = (scenario_id, event_index, phase_index)
        combinations.add(base)
        semantic_state = int(row.group_id.rsplit("-", 1)[-1])
        semantic_states.setdefault(base, set()).add(semantic_state)

    assert len(combinations) == 3 * len(OnboardEventKind) * len(ONBOARD_MODEL_TASK_PHASES)
    assert all(states == set(range(5)) for states in semantic_states.values())


def test_phase_augmentation_count_is_explicit_and_deterministic() -> None:
    rows = _samples(
        300,
        20261005,
        phase_augment=True,
        phase_augment_samples=3,
    )
    phase_rows = [row for row in rows if row.family.startswith("phase_")]
    expected = _phase_transition_samples(3, 20261005 + 2991)
    assert len(phase_rows) == len(expected)
    assert [row.family for row in phase_rows] == [row.family for row in expected]
    assert all(len(row.features) == 40 for row in phase_rows)


def test_phase_augmentation_covers_full_lifecycle_with_compatible_labels() -> None:
    rows = _phase_transition_samples(100, 20261005)
    families = {row.family for row in rows}
    assert {
        "phase_takeoff_target_present",
        "phase_search_target_mismatch",
        "phase_inspect_target_confirm",
        "phase_match_target_confirm",
        "phase_match_to_record_return",
        "phase_record_to_return",
        "phase_return_to_report",
        "phase_report_hold",
        "phase_hold_gps_degraded",
        "phase_land_battery_low",
    } <= families
    for row in rows:
        task = ONBOARD_MODEL_TASK_ACTION_ORDER[row.task_label]
        flight = ONBOARD_MODEL_ACTION_ORDER[row.flight_label]
        compatibility = {
            OnboardTaskAction.TAKEOFF: {OnboardFlightAction.GOTO_RELATIVE},
            OnboardTaskAction.SEARCH: {
                OnboardFlightAction.MOVE_RELATIVE,
                OnboardFlightAction.GOTO_RELATIVE,
                OnboardFlightAction.SEARCH,
                OnboardFlightAction.FOLLOW_TARGET,
            },
            OnboardTaskAction.INSPECT_TARGET: {
                OnboardFlightAction.GOTO_RELATIVE,
                OnboardFlightAction.FOLLOW_TARGET,
            },
            OnboardTaskAction.MATCH_TARGET: {
                OnboardFlightAction.GOTO_RELATIVE,
                OnboardFlightAction.FOLLOW_TARGET,
            },
            OnboardTaskAction.RECORD_LOCATION: {OnboardFlightAction.RETURN_HOME},
            OnboardTaskAction.RETURN_HOME: {OnboardFlightAction.RETURN_HOME},
            OnboardTaskAction.REPORT: {
                OnboardFlightAction.HOLD_POSITION,
                OnboardFlightAction.LAND,
            },
            OnboardTaskAction.HOLD: {OnboardFlightAction.HOLD_POSITION},
            OnboardTaskAction.LAND: {OnboardFlightAction.LAND},
        }
        assert flight in compatibility[task]


def test_phase_follow_teacher_uses_observed_bearing_and_range() -> None:
    rows = _phase_transition_samples(200, 20261006)
    follow_rows = [
        row
        for row in rows
        if row.family
        in {"phase_inspect_target_confirm", "phase_match_target_confirm"}
    ]
    assert follow_rows
    for row in follow_rows:
        distance_m = row.features[15] * 5_000.0
        bearing = row.features[14]
        north = math.tanh(row.regression[0])
        east = math.tanh(row.regression[1])
        assert distance_m == pytest.approx(math.hypot(north, east) * 500.0)
        assert north == pytest.approx(math.cos(bearing * math.pi) * distance_m / 500.0)
        assert east == pytest.approx(math.sin(bearing * math.pi) * distance_m / 500.0)


def test_semantic_match_regression_preserves_left_right_bearing() -> None:
    rows = _semantic_transition_samples(240, 20261005)
    match_rows = [
        row
        for row in rows
        if row.task_label == ONBOARD_MODEL_TASK_ACTION_ORDER.index(OnboardTaskAction.MATCH_TARGET)
    ]
    assert match_rows
    assert any(math.tanh(row.regression[1]) < -1.0e-3 for row in match_rows)
    assert any(math.tanh(row.regression[1]) > 1.0e-3 for row in match_rows)
    for row in match_rows:
        bearing = row.features[14]
        if abs(abs(bearing) - (1.0 / 3.0)) < 1.0e-6:
            assert math.tanh(row.regression[0]) > 0.0
            assert (math.tanh(row.regression[1]) > 0.0) == (bearing > 0.0)


def test_follow_teachers_bind_distance_to_navigation_magnitude() -> None:
    follow_label = ONBOARD_MODEL_ACTION_ORDER.index(OnboardFlightAction.FOLLOW_TARGET)
    rows = [_teacher(random.Random(seed)) for seed in range(5_000)]
    rows.extend(_semantic_transition_samples(240, 20261005))
    rows.extend(_temporal_transition_samples(240, 20261005))
    follow_rows = [row for row in rows if row.flight_label == follow_label]
    assert follow_rows
    for row in follow_rows:
        predicted_distance_fraction = math.hypot(
            math.tanh(row.regression[0]), math.tanh(row.regression[1])
        ) / 10.0
        assert row.features[15] == pytest.approx(predicted_distance_fraction, abs=1.0e-6)


def test_bearing_anchor_samples_are_symmetric_and_contract_safe() -> None:
    rows = _bearing_anchor_samples(80, 20261005)
    assert len(rows) == 80
    assert {row.family for row in rows} == {"bearing_anchor_left", "bearing_anchor_right"}
    assert len({row.group_id for row in rows}) == len(rows)
    assert len({abs(row.features[14]) for row in rows}) >= 5
    for row in rows:
        assert len(row.features) == 40
        bearing = row.features[14]
        north = math.tanh(row.regression[0])
        east = math.tanh(row.regression[1])
        assert -1.0 < north < 1.0
        assert (east > 0.0) == (bearing > 0.0)
        assert 8.0 / 5_000.0 <= row.features[15] <= 420.0 / 5_000.0
        distance_m = row.features[15] * 5_000.0
        assert north == pytest.approx(math.cos(bearing * math.pi) * distance_m / 500.0)
        assert east == pytest.approx(math.sin(bearing * math.pi) * distance_m / 500.0)


def test_semantic_bearing_and_lateral_label_share_one_coordinate() -> None:
    rows = _semantic_transition_samples(240, 20261005)
    checked = 0
    for row in rows:
        # Only moving/search rows carry a non-zero lateral regression target.
        east_m = math.tanh(row.regression[1])
        if abs(east_m) < 1.0e-6 or row.task_label != ONBOARD_MODEL_TASK_ACTION_ORDER.index(
            OnboardTaskAction.SEARCH
        ):
            continue
        bearing_fraction = row.features[14]
        assert (bearing_fraction > 0.0) == (east_m > 0.0)
        checked += 1
    assert checked > 0


def test_gazebo_3d_bearing_uses_runtime_degree_fraction() -> None:
    rows = _gazebo_3d_transition_samples(
        240,
        20261005,
        ROOT / "configs/scenarios/gazebo-3d-mainline-v1.manifest.json",
    )
    checked = 0
    for row in rows:
        north = math.tanh(row.regression[0])
        east = math.tanh(row.regression[1])
        # The bounded regression head intentionally clips very long routes at
        # +/-0.98 before applying atanh; only compare unsaturated samples to
        # the exact bearing identity.
        if abs(north) + abs(east) < 1.0e-6 or abs(north) >= 0.97 or abs(east) >= 0.97:
            continue
        expected_fraction = math.atan2(east, north) / math.pi
        assert row.features[14] == pytest.approx(expected_fraction, abs=1.0e-6)
        checked += 1
    assert checked > 0


def test_gazebo_3d_clear_obstacle_rows_stay_outside_hold_boundary() -> None:
    rows = _gazebo_3d_transition_samples(
        240,
        20261005,
        ROOT / "configs/scenarios/gazebo-3d-mainline-v1.manifest.json",
    )
    obstacle_event_index = list(OnboardEventKind).index(OnboardEventKind.OBSTACLE_RISK)
    for row in rows:
        if row.features[24 + obstacle_event_index] == 1.0:
            continue
        assert row.features[21] * 5_000.0 >= 0.10


def test_gazebo_3d_teacher_stays_inside_shared_regression_envelope() -> None:
    rows = _gazebo_3d_transition_samples(
        300,
        20261005,
        ROOT / "configs/scenarios/gazebo-3d-mainline-v1.manifest.json",
    )
    for row in rows:
        north_m = abs(math.tanh(row.regression[0]) * 500.0)
        east_m = abs(math.tanh(row.regression[1]) * 500.0)
        altitude_m = 120.0 / (1.0 + math.exp(-row.regression[2]))
        speed_mps = 50.0 / (1.0 + math.exp(-row.regression[3]))
        assert north_m <= 420.0
        assert east_m <= 420.0
        assert altitude_m <= 110.0
        assert speed_mps <= 32.0


def test_gazebo_3d_speed_target_is_not_copied_into_current_airspeed() -> None:
    rows = _gazebo_3d_transition_samples(
        240,
        20261005,
        ROOT / "configs/scenarios/gazebo-3d-mainline-v1.manifest.json",
    )
    mismatches = 0
    for row in rows:
        desired_speed = 50.0 / (1.0 + math.exp(-row.regression[3]))
        current_airspeed = row.features[3] * 50.0
        if abs(desired_speed - current_airspeed) > 1.0e-3:
            mismatches += 1
    assert mismatches > 0
