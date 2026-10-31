from __future__ import annotations

import copy
import hashlib
import io
import json
import math
from pathlib import Path

import pytest
from PIL import Image

pytest.importorskip("yaml", reason="OpenFly navigation dataset builder requires optional PyYAML")

from scripts import build_onboard_openfly_navigation_dataset as builder


def _action(index, pos, yaw, kind, value):
    return {"imageid": index, "pos": list(pos), "yaw": yaw, "type": kind, "value": value}


@pytest.mark.parametrize("yaw", [0, math.pi / 2, -math.pi / 2, math.pi])
def test_forward_is_body_forward_not_world_north(yaw):
    start = [1900.0, 950.0, -30.0]
    end = [start[0] + 3 * math.cos(yaw), start[1] + 3 * math.sin(yaw), start[2]]
    actions = [_action(0, start, yaw, "go straight", 3), _action(1, end, yaw, "stop", 0)]
    target, audit = builder.derive_primitive(actions, 0)
    assert target == {"kind": "translate", "translation_m": [3.0, 0.0, 0.0], "yaw_delta_rad": None}
    assert max(audit["position_abs_error_m"]) < 1e-9


@pytest.mark.parametrize("kind,dz,down", [("go up", 3, -3.0), ("go down", -3, 3.0)])
def test_vertical_source_up_becomes_body_down(kind, dz, down):
    actions = [_action(0, [0, 0, 20], 1.2, kind, 3), _action(1, [0, 0, 20 + dz], 1.2, "stop", 0)]
    target, _ = builder.derive_primitive(actions, 0)
    assert target["translation_m"] == [0.0, 0.0, down]


@pytest.mark.parametrize("kind,delta", [("turn left", math.pi / 6), ("turn right", -math.pi / 6)])
def test_turn_degree_value_and_wrapped_source_yaw_to_right_positive(kind, delta):
    yaw = 3.12
    actions = [
        _action(0, [0, 0, 0], yaw, kind, 30),
        _action(1, [0, 0, 0], builder.wrapped(yaw + delta), "stop", 0),
    ]
    target, audit = builder.derive_primitive(actions, 0)
    assert target["translation_m"] is None
    assert target["yaw_delta_rad"] == pytest.approx(-delta)
    assert audit["yaw_abs_error_rad"] < 1e-12


@pytest.mark.parametrize(
    "mutation", ["wrong_distance", "wrong_turn", "lateral", "unknown", "nan_yaw", "noncontiguous"]
)
def test_metric_disagreement_is_rejected_not_repaired(mutation):
    actions = [_action(0, [0, 0, 0], 0, "go straight", 3), _action(1, [3, 0, 0], 0, "stop", 0)]
    if mutation == "wrong_distance":
        actions[0]["value"] = 9
    elif mutation == "wrong_turn":
        actions[1]["yaw"] = 0.1
    elif mutation == "lateral":
        actions[1]["pos"][1] = 0.01
    elif mutation == "unknown":
        actions[0]["type"] = "land"
    elif mutation == "nan_yaw":
        actions[0]["yaw"] = float("nan")
    else:
        actions[1]["imageid"] = 4
    with pytest.raises(builder.NavigationDatasetError):
        builder.derive_primitive(actions, 0)


def test_terminal_stop_does_not_generate_landing_or_duration():
    action = _action(0, [1, 2, 3], 0, "stop", 0)
    target, audit = builder.derive_primitive([action], 0)
    assert target == {"kind": "stop", "translation_m": None, "yaw_delta_rad": None}
    assert audit["next_imageid"] is None
    assert audit["landing_or_task_completion_inferred"] is False
    with pytest.raises(builder.NavigationDatasetError):
        builder.derive_primitive([action, copy.deepcopy(action)], 0)


def _visual(value):
    return {"rgb_sha256": f"{value:064x}", "dhash64": f"{value:016x}", "mean_rgb": [value * 30] * 3}


def test_scenes_and_visual_duplicates_group_before_split():
    episodes = [
        {
            "episode_id": str(i),
            "scene_id": f"s{i}",
            "source_episode": str(i),
            "visual_identities": [_visual(i + 1)],
        }
        for i in range(4)
    ]
    episodes[1]["scene_id"] = "s0"
    episodes[2]["visual_identities"] = episodes[3]["visual_identities"]
    assignment, report = builder.assign_partitions(episodes)
    assert report["group_count"] == 2
    assert set(assignment.values()) == {"train"}
    assert report["independent_development_holdout_available"] is False
    assert report["visual_duplicate_edges"]


def test_three_independent_groups_get_frozen_whole_episode_development_splits():
    episodes = [
        {
            "episode_id": str(i),
            "scene_id": f"s{i}",
            "source_episode": str(i),
            "visual_identities": [_visual(i + 1)],
        }
        for i in range(3)
    ]
    assignment, report = builder.assign_partitions(episodes)
    assert assignment == {"0": "train", "1": "dev-validation", "2": "dev-test"}
    assert report["independent_development_holdout_available"] is True


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return builder.binding(path)


def _fixture(tmp_path, monkeypatch, *, scale=1, scene="env_airsim_18"):
    episode = f"{scene}/astar_data/low_short/original_episode"
    record = {
        "image_path": episode,
        "gpt_instruction": "Original instruction; stop at the building.\n",
        "action": [9, 0],
        "index_list": ["20250108_190201_1", "20250108_190202_2"],
        "pos": [[3.0, 0.0, 0.0], [6.0, 0.0, 0.0]],
        "yaw": [0.0, 0.0],
    }
    annotation = _write(tmp_path / "raw/train.json", json.dumps([record]).encode())
    monkeypatch.setattr(builder, "ANNOTATION_BYTES", annotation["bytes"])
    monkeypatch.setattr(builder, "ANNOTATION_SHA256", annotation["sha256"])
    actions = [
        _action(
            i, [i * 3.0, 0.0, 0.0], 0.0, "stop" if i == 2 else "go straight", 0 if i == 2 else 3
        )
        for i in range(3)
    ]
    pose = _write(
        tmp_path / "raw/pose.jsonl",
        "\n".join(json.dumps({"action": action}, indent=2) for action in actions).encode()
        + b'\n{"aim_landmark":{"position":[99,98,97]}}\n',
    )
    images = []
    for i in range(3):
        buffer = io.BytesIO()
        Image.new("RGB", (16, 12), (i * 50, 20, 30)).save(buffer, format="PNG")
        image = _write(tmp_path / f"raw/{i}.png", buffer.getvalue())
        image.update(
            {
                "imageid": i,
                "repository_path": f"Image/{episode}/20250108_19020{i}_{i}.png",
                "rgb": {"width": 16, "height": 12, "encoding": "png"},
            }
        )
        images.append(image)
    # Filename alignment is exact even though aggregated code9 is not raw3m.
    images[1]["repository_path"] = f"Image/{episode}/20250108_190201_1.png"
    images[2]["repository_path"] = f"Image/{episode}/20250108_190202_2.png"
    code = [
        _write(tmp_path / "code" / name, b"fixture-generation-code-not-external-evidence\n")
        for name in builder.CODE_PATHS
    ]
    code.append(
        _write(
            tmp_path / f"code/configs/{scene}.yaml",
            f"traj_map:\n  traj_scale_ratio: {scale}\n  pcd_scale_ratio: 1\n".encode(),
        )
    )
    license_file = _write(tmp_path / "licenses/README.md", b"---\nlicense: apache-2.0\n---\n")
    extracted = _write(tmp_path / "selected/0.json", json.dumps(record).encode())
    manifest = {
        "contract": "multidetect.openfly-navigation-pilot-v1",
        "dataset": {"repository": builder.DATASET, "revision": builder.REVISION, "split": "train"},
        "code": {"revision": builder.CODE_REVISION},
        "source_planned_not_executed": True,
        "seen_unseen_downloaded": False,
        "weights_downloaded": False,
        "annotation": annotation,
        "code_sources": code,
        "license_sources": [license_file],
        "metadata_records": [],
        "source_files": [],
        "selection": {"annotation_record_count": 1},
        "episodes": [
            {
                "annotation_index": 0,
                "image_path": episode,
                "annotation_record": extracted,
                "pose": pose,
                "images": images,
            }
        ],
    }
    hf_tree, git_tree = [], []

    def published(record, repository_path, *, code_file=False):
        raw = Path(record["path"]).read_bytes()
        digest = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        record.update(
            {
                "repository_path": repository_path,
                "official_digest_kind": "sha1-git-blob",
                "official_digest": digest,
                "source_url": (
                    f"https://raw.githubusercontent.com/SHAILAB-IPEC/OpenFly-Platform/{builder.CODE_REVISION}/"
                    if code_file
                    else f"https://huggingface.co/datasets/{builder.DATASET}/resolve/{builder.REVISION}/"
                )
                + repository_path,
            }
        )
        if code_file:
            git_tree.append(
                {"path": repository_path, "type": "blob", "size": len(raw), "sha": digest}
            )
        else:
            hf_tree.append(
                {"path": repository_path, "type": "file", "size": len(raw), "oid": digest}
            )

    published(annotation, "Annotation/train.json")
    published(pose, f"Image/{episode}/pose.jsonl")
    published(license_file, "README.md")
    for image in images:
        published(image, image["repository_path"])
    for code_record, repository_path in zip(
        code, [*builder.CODE_PATHS, f"configs/{scene}.yaml"], strict=True
    ):
        published(code_record, repository_path, code_file=True)
    manifest["metadata_records"] = [
        _write(tmp_path / "metadata/hf-tree.json", json.dumps(hf_tree).encode()),
        _write(
            tmp_path / "metadata/code-tree.json",
            json.dumps(
                {"sha": builder.CODE_REVISION, "truncated": False, "tree": git_tree}
            ).encode(),
        ),
    ]
    manifest_path = tmp_path / "source-manifest.json"
    _write(manifest_path, json.dumps(manifest).encode())
    return manifest_path, manifest


def test_complete_original_step_dataset_native_cpu(tmp_path, monkeypatch):
    source, _ = _fixture(tmp_path, monkeypatch)
    out = tmp_path / "dataset"
    report = builder.build_dataset(source, out, artifact_root=tmp_path)
    assert report["admitted_sample_count"] == 3
    assert report["primitive_counts"] == {"translate": 2, "stop": 1}
    assert report["partition_counts"] == {"train": 3}
    assert report["training_ready"] is False
    assert report["sources_unchanged"] is True
    assert report["aggregate_annotation_actions_used_as_gold"] is False
    assert report["dt_available"] is False
    samples = [json.loads(line) for line in (out / "samples.jsonl").read_text().splitlines()]
    assert samples[0]["planned_action"]["translation_m"] == [3.0, 0.0, 0.0]
    assert samples[1]["planned_action"]["translation_m"] == [3.0, 0.0, 0.0]
    assert [frame["step_index"] for frame in samples[1]["frames"]] == [0, 1]
    assert len(samples[0]["frames"]) == 1
    assert samples[-1]["alignment"]["next_step"] is None
    assert samples[0]["prompt"]["text_original"].endswith("\n")
    assert samples[0]["planned_action"]["raw_source_pointer"]["format"] == "json_sequence"
    with pytest.raises(builder.NavigationDatasetError, match="new directory"):
        builder.build_dataset(source, out, artifact_root=tmp_path)


@pytest.mark.parametrize("scale,scene", [(2, "env_airsim_18"), (1, "env_gs_nwpu01")])
def test_unreviewed_scale_or_renderer_does_not_gain_metric_labels(
    tmp_path, monkeypatch, scale, scene
):
    source, _ = _fixture(tmp_path, monkeypatch, scale=scale, scene=scene)
    with pytest.raises(builder.NavigationDatasetError, match="no complete raw episode"):
        builder.build_dataset(source, tmp_path / "out", artifact_root=tmp_path)


def test_manifest_source_hash_and_actual_png_mutation_are_rejected(tmp_path, monkeypatch):
    source, manifest = _fixture(tmp_path, monkeypatch)
    with pytest.raises(builder.NavigationDatasetError, match="frozen expected SHA"):
        builder.build_dataset(
            source, tmp_path / "out", artifact_root=tmp_path, source_sha256="0" * 64
        )
    Path(manifest["episodes"][0]["images"][1]["path"]).write_bytes(b"not-original")
    with pytest.raises(builder.NavigationDatasetError, match="actual size/SHA"):
        builder.build_dataset(source, tmp_path / "out", artifact_root=tmp_path)


def test_rehashed_extracted_prompt_does_not_override_original_source(tmp_path, monkeypatch):
    source, manifest = _fixture(tmp_path, monkeypatch)
    extracted = Path(manifest["episodes"][0]["annotation_record"]["path"])
    record = json.loads(extracted.read_bytes())
    record["gpt_instruction"] = "Oracle GPS coordinate 99 98 97"
    extracted.write_bytes(json.dumps(record).encode())
    manifest["episodes"][0]["annotation_record"] = builder.binding(extracted)
    source.write_bytes(json.dumps(manifest).encode())
    with pytest.raises(builder.NavigationDatasetError, match="differs from full source"):
        builder.build_dataset(source, tmp_path / "out", artifact_root=tmp_path)


def test_self_consistent_local_rehash_does_not_replace_published_bytes(tmp_path, monkeypatch):
    source, manifest = _fixture(tmp_path, monkeypatch)
    pose = manifest["episodes"][0]["pose"]
    pose_path = Path(pose["path"])
    pose_path.write_bytes(pose_path.read_bytes() + b"\n")
    pose.update(builder.binding(pose_path))
    source.write_bytes(json.dumps(manifest).encode())
    with pytest.raises(builder.NavigationDatasetError, match="publisher digest/size"):
        builder.build_dataset(source, tmp_path / "out", artifact_root=tmp_path)
