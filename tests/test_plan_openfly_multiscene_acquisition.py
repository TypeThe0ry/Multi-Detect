from __future__ import annotations

import copy
import hashlib
import io
import sys
from types import SimpleNamespace

import pytest
from PIL import Image

from scripts import plan_openfly_multiscene_acquisition as planner


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value if isinstance(value, bytes) else planner.canonical(value))
    return planner.binding(path)


def git_sha(payload):
    return hashlib.sha1(f"blob {len(payload)}\0".encode() + payload).hexdigest()


def payload_entry(path, payload):
    return {"type": "file", "path": path, "size": len(payload), "oid": git_sha(payload)}


def annotation(scene, name):
    return {
        "image_path": f"{scene}/astar_data/low_short/{name}",
        "gpt_instruction": f"Inspect the building on route {name}.",
        "action": [9, 0],
        "index_list": ["20250108_190201_0", "20250108_190202_1"],
        "pos": [[0.0, 0.0, 0.0], [3.0, 0.0, 0.0]],
        "yaw": [0.0, 0.0],
    }


def route_assets(root, row, index, color):
    prefix = "Image/" + row["image_path"] + "/"
    actions = [
        {"action": {"imageid": 0, "pos": [0, 0, 0], "type": "go straight", "value": 3, "yaw": 0}},
        {"action": {"imageid": 1, "pos": [3, 0, 0], "type": "stop", "value": 0, "yaw": 0}},
        {"aim_landmark": [3, 0, 0]},
    ]
    raw = b"\n".join(planner.canonical(value) for value in actions)
    pose = write(root / "raw" / prefix / "pose.jsonl", raw)
    pose["repository_path"] = prefix + "pose.jsonl"
    tree = [payload_entry(pose["repository_path"], raw)]
    images = []
    for image_id, name in enumerate(row["index_list"]):
        buffer = io.BytesIO()
        with Image.new("RGB", (16, 16), color) as image:
            image.save(buffer, format="PNG")
        payload = buffer.getvalue()
        path = prefix + name + ".png"
        record = write(root / "raw" / path, payload)
        images.append({**record, "repository_path": path, "imageid": image_id})
        tree.append(payload_entry(path, payload))
    tree_binding = write(root / f"metadata/episode-{index}-tree.json", tree)
    return pose, images, tree_binding


@pytest.fixture
def local_source(tmp_path, monkeypatch):
    monkeypatch.setattr(planner, "ROOT", tmp_path)
    fetch, builder = planner.helpers()
    source = tmp_path / "artifacts/training/old-source"
    acquisition = tmp_path / "artifacts/training/openfly-multiscene-source-new"
    rows = [annotation("env_airsim_18", f"filler-{i}") for i in range(12)]
    for i in (0, 7, 8):
        rows[i] = annotation("env_airsim_18", f"historical-{i}")
    rows[9] = annotation("env_airsim_26", "new-train")
    rows[10] = annotation("env_airsim_16", "new-val")
    rows[11] = annotation("env_airsim_23", "new-test")
    original = write(source / "raw/Annotation/train.json", rows)
    monkeypatch.setattr(fetch, "ANNOTATION_BYTES", original["bytes"])
    monkeypatch.setattr(fetch, "ANNOTATION_SHA256", original["sha256"])
    calls = []
    monkeypatch.setattr(builder, "verify_published_sources", lambda *args: calls.append(args))
    for name in planner.SOURCE_FILES:
        write(tmp_path / name, b"fixture implementation source\n")
    config = b"traj_map:\n  traj_scale_ratio: 1\n  pcd_scale_ratio: 1\n"
    config_binding = write(source / "code/configs/env_airsim_18.yaml", config)
    code_entries = [
        {
            "path": "configs/" + scene + ".yaml",
            "type": "blob",
            "size": len(config),
            "sha": git_sha(config),
        }
        for scene in ("env_airsim_18", "env_airsim_26", "env_airsim_16", "env_airsim_23")
    ]
    code_tree = write(
        source / "metadata/code-tree.json",
        {"sha": fetch.CODE_REVISION, "truncated": False, "tree": code_entries},
    )
    episodes, metadata = [], [code_tree]
    for index in (0, 7, 8):
        pose, images, tree = route_assets(source, rows[index], index, (20, 0, 0))
        metadata.append(tree)
        episodes.append(
            {
                "annotation_index": index,
                "scene": "env_airsim_18",
                "image_path": rows[index]["image_path"],
                "annotation_record": write(
                    source / f"selected/annotation-{index}.json", rows[index]
                ),
                "pose": pose,
                "images": images,
            }
        )
    manifest = {
        "contract": "multidetect.openfly-navigation-pilot-v1",
        "dataset": {"repository": fetch.DATASET, "revision": fetch.REVISION, "split": "train"},
        "code": {"revision": fetch.CODE_REVISION},
        "seen_unseen_downloaded": False,
        "source_planned_not_executed": True,
        "annotation": original,
        "episodes": episodes,
        "metadata_records": metadata,
        "code_sources": [config_binding],
        "license_sources": [],
        "source_files": [],
    }
    manifest_path = source / "manifest.json"
    write(manifest_path, manifest)
    return SimpleNamespace(
        source=source,
        acquisition=acquisition,
        rows=rows,
        manifest=manifest,
        manifest_path=manifest_path,
        config=config,
        calls=calls,
    )


def proposal(source):
    return planner.build_plan(
        source.manifest_path,
        [("env_airsim_26", 9)],
        [("env_airsim_16", 10)],
        [("env_airsim_23", 11)],
        source.acquisition,
    )


def saved_plan(source):
    value = proposal(source)
    path = source.source.parent / "acquisition-plan.json"
    record = write(path, value)
    return path, record["sha256"], value


@pytest.mark.parametrize("raw", ['{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}'])
def test_strict_json_rejects_ambiguity(raw):
    with pytest.raises(ValueError):
        planner.strict_json(raw)


@pytest.mark.parametrize("value", ["env_airsim_26:9", "env_airsim_sh:220"])
def test_explicit_scene_route(value):
    scene, index = planner.parse_scene_route(value)
    assert scene.startswith("env_airsim_") and type(index) is int


@pytest.mark.parametrize(
    "value",
    [
        "scene:9",
        "env_airsim_26:-1",
        "../env_airsim_26:9",
        "env_airsim_26:9:10",
        "env_airsim_26:True",
    ],
)
def test_unsafe_or_inferred_scene_route_rejected(value):
    with pytest.raises(ValueError):
        planner.parse_scene_route(value)


def test_missing_raw_is_not_an_independent_dataset(local_source):
    path, digest, plan = saved_plan(local_source)
    result = planner.preflight(path, digest)
    assert local_source.calls
    assert result["complete_route_count"] == 3
    assert result["missing_route_count"] == 3
    assert result["verified_steps_by_effective_partition"] == {
        "train": 6,
        "dev-validation": 0,
        "dev-test": 0,
        "quarantine-visual-overlap": 0,
    }
    assert result["sources_before"] == result["sources_after"]
    assert result["independent_holdout_available"] is False
    assert result["visual_split_complete"] is False
    assert result["samples_created"] is False
    assert result["download_performed"] is False
    assert result["optimizer_performed"] is False
    assert not local_source.acquisition.exists()
    assert plan["scene_assignment"]["env_airsim_18"] == "train"
    assert all(row["status"] == "not_acquired" for row in result["routes"][3:])
    # Sparse annotation filenames are real; unlisted raw timestamps stay unknown.
    assert result["routes"][3]["required_frames"][0]["repository_path"].endswith(
        "20250108_190201_0.png"
    )
    assert result["routes"][3]["required_frames"][0]["bytes"] is None


@pytest.mark.parametrize(
    "case",
    [
        "move_history",
        "same_val_test_scene",
        "same_index",
        "wrong_declared_scene",
        "only_one_train",
        "sealed_destination",
        "omit_history",
    ],
)
def test_split_plan_cannot_reassign_training_or_invent_independence(local_source, case):
    train, validation, test = (
        [("env_airsim_26", 9)],
        [("env_airsim_16", 10)],
        [("env_airsim_23", 11)],
    )
    destination = local_source.acquisition
    if case == "move_history":
        validation = [("env_airsim_18", 10)]
    elif case == "same_val_test_scene":
        test = [("env_airsim_16", 11)]
    elif case == "same_index":
        test = [("env_airsim_23", 10)]
    elif case == "wrong_declared_scene":
        train = [("env_airsim_27", 9)]
    elif case == "only_one_train":
        train.clear()
    elif case == "sealed_destination":
        destination = local_source.source / "new-data"
    else:
        manifest = copy.deepcopy(local_source.manifest)
        manifest["episodes"].pop()
        write(local_source.manifest_path, manifest)
    with pytest.raises(ValueError):
        planner.build_plan(local_source.manifest_path, train, validation, test, destination)


@pytest.mark.parametrize(
    "case",
    [
        "wrong_sha",
        "claim",
        "historical_partition",
        "source_omitted",
        "route_omitted",
        "budget_bool",
        "unknown_field",
        "pose_changed",
        "config_changed",
        "entry_changed",
    ],
)
def test_admission_and_reopen_reject_drift(local_source, case):
    path, digest, value = saved_plan(local_source)
    if case == "wrong_sha":
        digest = "0" * 64
    elif case == "pose_changed":
        pose = local_source.source / "raw/Image" / local_source.rows[0]["image_path"] / "pose.jsonl"
        pose.write_bytes(b"changed pose")
    elif case == "config_changed":
        (local_source.source / "code/configs/env_airsim_18.yaml").write_bytes(b"forged scale")
    elif case == "entry_changed":
        (planner.ROOT / planner.SOURCE_FILES[0]).write_bytes(b"changed implementation")
    else:
        if case == "claim":
            value["claims"]["training_ready"] = True
        elif case == "historical_partition":
            value["scene_assignment"]["env_airsim_18"] = "dev-test"
        elif case == "source_omitted":
            value["source_files"].pop()
        elif case == "route_omitted":
            value["routes"].pop(0)
        elif case == "budget_bool":
            value["budget"]["max_routes"] = True
        else:
            value["model_score"] = 1.0
        digest = write(path, value)["sha256"]
    with pytest.raises(ValueError):
        planner.preflight(path, digest)


def visual(red, value):
    return {"rgb_sha256": f"{value:064x}", "dhash64": "0000000000000000", "mean_rgb": [red, 0, 0]}


def group_episode(key, scene, part, image):
    return {
        "episode_id": key,
        "scene_id": scene,
        "source_episode": key,
        "declared_partition": part,
        "visual_identities": [image],
    }


@pytest.mark.parametrize("order", [False, True])
def test_visual_overlap_anchors_train_not_sorted_group_hash(order):
    rows = [
        group_episode("z-history", "env_airsim_18", "train", visual(20, 1)),
        group_episode("a-val", "env_airsim_16", "dev-validation", visual(20, 1)),
        group_episode("b-test", "env_airsim_23", "dev-test", visual(180, 2)),
        group_episode("q-train", "env_airsim_26", "train", visual(100, 3)),
    ]
    if order:
        rows.reverse()
    result = planner.anchored_visual_split(rows)
    assert result["assignment"] == {
        "a-val": "train",
        "z-history": "train",
        "b-test": "dev-test",
        "q-train": "train",
    }
    assert result["overlap_routes"] == ["a-val"]


def test_validation_test_visual_overlap_quarantines_both():
    result = planner.anchored_visual_split(
        [
            group_episode("history", "env_airsim_18", "train", visual(20, 1)),
            group_episode("val", "env_airsim_16", "dev-validation", visual(100, 2)),
            group_episode("test", "env_airsim_23", "dev-test", visual(105, 3)),
        ]
    )
    assert result["assignment"]["val"] == "quarantine-visual-overlap"
    assert result["assignment"]["test"] == "quarantine-visual-overlap"


def test_missing_tree_does_not_invent_unspecified_rgb_filename(local_source):
    plan = proposal(local_source)
    row = copy.deepcopy(local_source.rows[9])
    row["index_list"][-1] = "20250108_190202_2"
    result = planner.inspect_route(plan["routes"][3], row, planner.BUDGET)
    assert result["required_raw_image_ids"] == [0, 1, 2]
    assert result["required_frames"][1]["repository_path"] is None
    assert result["eligible_step_count"] == 0


def test_terminal_id_is_bounded_before_range_allocation(local_source):
    row = copy.deepcopy(local_source.rows[9])
    row["index_list"][-1] = "20250108_190202_999999999"
    with pytest.raises(ValueError, match="budget"):
        planner.inspect_route(proposal(local_source)["routes"][3], row, planner.BUDGET)


def test_all_new_assets_are_reopened_and_visual_split_is_real(local_source):
    colors = [(0, 80, 0), (0, 0, 150), (220, 220, 0)]
    for index, color in zip((9, 10, 11), colors, strict=True):
        route_assets(local_source.acquisition, local_source.rows[index], index, color)
        scene = local_source.rows[index]["image_path"].split("/")[0]
        write(local_source.acquisition / "code/configs" / (scene + ".yaml"), local_source.config)
    path, digest, _value = saved_plan(local_source)
    result = planner.preflight(path, digest)
    assert result["complete_route_count"] == 6
    assert result["independent_holdout_available"] is True
    assert result["verified_steps_by_effective_partition"] == {
        "train": 8,
        "dev-validation": 2,
        "dev-test": 2,
        "quarantine-visual-overlap": 0,
    }
    all_bound_paths = {row["path"] for row in result["sources_after"]}
    assert any(
        "openfly-multiscene-source-new/raw/Image/env_airsim_16" in path for path in all_bound_paths
    )
    assert result["training_ready"] is False
    assert result["quality_claim"] is False


@pytest.mark.parametrize(
    "case",
    ["pose_without_rgb", "wrong_metric", "png_changed", "wrong_config_publisher", "nonunit_scale"],
)
def test_real_local_payload_checks_do_not_accept_metadata_only(local_source, case):
    pose, images, _tree = route_assets(
        local_source.acquisition, local_source.rows[9], 9, (0, 80, 0)
    )
    write(local_source.acquisition / "code/configs/env_airsim_26.yaml", local_source.config)
    if case == "pose_without_rgb":
        planner.local_path(images[0]["path"]).unlink()
    elif case == "wrong_metric":
        path = planner.local_path(pose["path"])
        raw = path.read_bytes().replace(b'"value":3', b'"value":9')
        path.write_bytes(raw)
        tree_path = local_source.acquisition / "metadata/episode-9-tree.json"
        tree = planner.strict_json(tree_path.read_bytes())
        tree[0] = payload_entry(pose["repository_path"], raw)
        write(tree_path, tree)
    elif case == "png_changed":
        planner.local_path(images[0]["path"]).write_bytes(b"not the original PNG")
    elif case == "wrong_config_publisher":
        write(local_source.acquisition / "code/configs/env_airsim_26.yaml", b"traj_map: {}")
    else:
        # Bind a genuinely published but unreviewed nonunit configuration in this fixture.
        config = local_source.config.replace(b"ratio: 1", b"ratio: 2")
        write(local_source.acquisition / "code/configs/env_airsim_26.yaml", config)
        tree_path = local_source.source / "metadata/code-tree.json"
        tree = planner.strict_json(tree_path.read_bytes())
        entry = next(row for row in tree["tree"] if row["path"] == "configs/env_airsim_26.yaml")
        entry.update(sha=git_sha(config), size=len(config))
        changed = write(tree_path, tree)
        manifest = copy.deepcopy(local_source.manifest)
        manifest["metadata_records"][0] = changed
        write(local_source.manifest_path, manifest)
    path, digest, _value = saved_plan(local_source)
    if case in {"pose_without_rgb", "nonunit_scale"}:
        result = planner.preflight(path, digest)
        assert result["routes"][3]["eligible_step_count"] == 0
        assert result["independent_holdout_available"] is False
    else:
        with pytest.raises(ValueError):
            planner.preflight(path, digest)


def test_report_create_only_and_protected_path(tmp_path, monkeypatch):
    monkeypatch.setattr(planner, "ROOT", tmp_path)
    (tmp_path / "artifacts").mkdir()
    report = tmp_path / "artifacts/preflight-plan/new.json"
    planner.write_report(report, {"status": "dry-run"})
    with pytest.raises(ValueError):
        planner.write_report(report, {})
    with pytest.raises(ValueError):
        planner.write_report(tmp_path / "configs/protected.json", {})
    assert planner.strict_json(report.read_bytes()) == {"status": "dry-run"}


def test_external_path_cannot_be_admitted(tmp_path, monkeypatch):
    (tmp_path / "repo").mkdir()
    monkeypatch.setattr(planner, "ROOT", tmp_path / "repo")
    with pytest.raises(ValueError):
        planner.local_path(tmp_path / "outside.json", exists=False)


def test_helpers_restore_import_search_path():
    original = sys.path.copy()
    planner.helpers()
    assert sys.path == original


def test_new_raw_source_is_rehashed_after_visual_decode(local_source, monkeypatch):
    route_assets(local_source.acquisition, local_source.rows[9], 9, (0, 80, 0))
    write(local_source.acquisition / "code/configs/env_airsim_26.yaml", local_source.config)
    path, digest, _value = saved_plan(local_source)
    _, builder = planner.helpers()
    original = builder.rgb_identity

    def decode_then_change(image_path):
        value = original(image_path)
        if "openfly-multiscene-source-new" in str(image_path):
            image_path.write_bytes(image_path.read_bytes() + b"changed after visual verification")
        return value

    monkeypatch.setattr(builder, "rgb_identity", decode_then_change)
    with pytest.raises(ValueError, match="Sources changed"):
        planner.preflight(path, digest)


@pytest.mark.parametrize("conflict", [False, True])
def test_retained_declaration_cannot_be_overwritten_by_actual_source(local_source, conflict):
    manifest = copy.deepcopy(local_source.manifest)
    entry = planner.binding(planner.ROOT / planner.SOURCE_FILES[0])
    entry["path"] = str(planner.local_path(entry["path"]))
    if conflict:
        entry["sha256"] = "0" * 64
    manifest["source_files"].append(entry)
    write(local_source.manifest_path, manifest)
    path, digest, _value = saved_plan(local_source)
    if conflict:
        with pytest.raises(ValueError, match="Conflicting canonical source declaration"):
            planner.preflight(path, digest)
    else:
        result = planner.preflight(path, digest)
        paths = [row["path"] for row in result["sources_before"]]
        assert paths.count(planner.SOURCE_FILES[0]) == 1


def test_actual_plan_is_rehashed_after_use(local_source, monkeypatch):
    path, digest, _value = saved_plan(local_source)
    _, builder = planner.helpers()
    original = builder.rgb_identity
    changed = False

    def decode_then_change_plan(image_path):
        nonlocal changed
        value = original(image_path)
        if not changed:
            path.write_bytes(path.read_bytes() + b" ")
            changed = True
        return value

    monkeypatch.setattr(builder, "rgb_identity", decode_then_change_plan)
    with pytest.raises(ValueError, match="Sources changed"):
        planner.preflight(path, digest)
