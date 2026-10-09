import json

import pytest

from scripts import audit_openfly_multiscene_navigation_geometry as audit


def pose_and_metadata():
    actions = [
        {"imageid": 0, "pos": [0.0, 0.0, 0.0], "yaw": 0.0, "type": "go straight", "value": 3},
        {"imageid": 1, "pos": [3.0, 0.0, 0.0], "yaw": 0.0, "type": "stop", "value": 0},
    ]
    metadata = [
        {
            "frame_index": a["imageid"],
            "pos": a["pos"].copy(),
            "yaw": a["yaw"],
            "action_type": a["type"],
            "action_value": a["value"],
        }
        for a in actions
    ]
    return actions, metadata


def test_geometry_derives_each_immediate_original_step_only():
    from scripts.build_onboard_openfly_navigation_dataset import derive_primitive

    actions, metadata = pose_and_metadata()
    result = audit.metric_targets(actions, metadata, derive_primitive)
    assert result[0][0] == {
        "kind": "translate",
        "translation_m": [3.0, 0.0, 0.0],
        "yaw_delta_rad": None,
    }
    assert result[1][0] == {"kind": "stop", "translation_m": None, "yaw_delta_rad": None}
    assert result[1][1]["landing_or_task_completion_inferred"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("frame_index", True),
        ("frame_index", 1),
        ("pos", [3, 0, 0]),
        ("yaw", 0.1),
        ("action_type", "turn left"),
        ("action_value", 4),
    ],
)
def test_metadata_mismatch_is_rejected_before_any_derivation(field, value):
    actions, metadata = pose_and_metadata()
    metadata[0][field] = value
    calls = []
    with pytest.raises(ValueError, match="differs"):
        audit.metric_targets(actions, metadata, lambda *args: calls.append(args))
    assert calls == []


@pytest.mark.parametrize("actions,metadata", [([], []), ([{}], []), ([], [{}])])
def test_complete_nonempty_geometry_required(actions, metadata):
    with pytest.raises(ValueError, match="lengths"):
        audit.metric_targets(actions, metadata, lambda *args: None)


def test_original_primitive_pose_conflict_cannot_be_fitted():
    from scripts.build_onboard_openfly_navigation_dataset import derive_primitive

    actions, metadata = pose_and_metadata()
    actions[1]["pos"][0] = metadata[1]["pos"][0] = 4.0
    with pytest.raises(ValueError, match="disagreement"):
        audit.metric_targets(actions, metadata, derive_primitive)


def frame(path, rgb, dhash, mean):
    return {"path": path, "rgb_sha256": rgb, "dhash64": f"{dhash:016x}", "mean_rgb": mean}


@pytest.mark.parametrize(
    "distance,mean,expected", [(3, 10, True), (4, 10, False), (3, 11, False), (0, 0, True)]
)
def test_frozen_near_duplicate_boundary(distance, mean, expected):
    groups = [
        {"scene_id": "a", "frames": [frame("a.png", "a", 0, [0, 0, 0])]},
        {"scene_id": "b", "frames": [frame("b.png", "b", (1 << distance) - 1, [mean, 0, 0])]},
    ]
    assert bool(audit.visual_edges(groups)) is expected


def test_exact_rgb_always_forms_edge_and_comparison_retains_all_pairs():
    groups = [
        {"scene_id": "a", "frames": [frame("a.png", "same", 0, [0, 0, 0])]},
        {
            "scene_id": "b",
            "frames": [
                frame("b.png", "same", (1 << 64) - 1, [255, 255, 255]),
                frame("c.png", "same", 0, [0, 0, 0]),
            ],
        },
    ]
    edges = audit.visual_edges(groups)
    assert len(edges) == 1 and len(edges[0]["matches"]) == 2
    assert all(row["exact_rgb"] for row in edges[0]["matches"])


@pytest.mark.parametrize("change", ["sha", "bytes", "escape", "bool_bytes", "cap"])
def test_source_binding_fails_closed(tmp_path, monkeypatch, change):
    root = tmp_path / "repo"
    root.mkdir()
    path = root / "source"
    path.write_bytes(b"original")
    monkeypatch.setattr(audit, "ROOT", root)
    row = audit.record(path)
    if change == "sha":
        row["sha256"] = "0" * 64
    elif change == "bytes":
        row["bytes"] += 1
    elif change == "escape":
        outside = tmp_path / "outside"
        outside.write_bytes(b"original")
        row = audit.record(outside)
    elif change == "bool_bytes":
        row["bytes"] = True
    with pytest.raises(ValueError):
        audit.verified(row, cap=1 if change == "cap" else 128)


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}'])
def test_small_json_does_not_accept_ambiguous_values(tmp_path, monkeypatch, raw):
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    path = tmp_path / "source.json"
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        audit.read_small(audit.record(path))


def test_json_case_sensitive_keys_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    path = tmp_path / "source.json"
    path.write_bytes(b'{"GPU_used":false,"gpu_used":false}')
    assert set(audit.read_small(audit.record(path))) == {"GPU_used", "gpu_used"}
    assert json.loads(audit.canonical({"one": 1})) == {"one": 1}


def test_existing_output_is_not_modified(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    output = tmp_path / "artifacts/diagnostics/existing"
    output.mkdir(parents=True)
    marker = output / "marker"
    marker.write_bytes(b"preserved")
    with pytest.raises(FileExistsError):
        audit.audit(output)
    assert marker.read_bytes() == b"preserved"


def test_not_training_or_hardware_authority():
    assert set(audit.FALSE_CLAIMS) >= {
        "training_ready",
        "samples_created",
        "model_loaded",
        "independent_holdout_verified",
        "candidate_promoted",
        "execution_authority",
        "final_release_allowed",
    }
