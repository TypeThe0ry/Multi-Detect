from __future__ import annotations

import copy
import hashlib
import io
import sys
from types import SimpleNamespace

import pytest
from PIL import Image

from scripts import fetch_openfly_multiscene_navigation as acquisition
from scripts import plan_openfly_multiscene_acquisition as planner


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value if isinstance(value, bytes) else planner.canonical(value) + b"\n")
    return planner.binding(path)


def publisher_entry(path, raw, *, kind="file"):
    digest = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
    return {
        "type": kind,
        "path": path,
        "size": len(raw),
        "sha" if kind == "blob" else "oid": digest,
    }


class Response:
    def __init__(self, raw):
        self.raw = raw
        self.headers = {"Content-Length": str(len(raw))}
        self.closed = False

    def iter_content(self, _size):
        yield self.raw

    def close(self):
        self.closed = True


class Client:
    def __init__(self, trees, payloads):
        self.trees = trees
        self.payloads = payloads
        self.tree_calls = []
        self.stream_calls = []
        self.responses = []
        self.before_stream = None
        self.after_tree = None

    def tree(self, path):
        self.tree_calls.append(path)
        if self.after_tree is not None:
            self.after_tree(path)
        tree = copy.deepcopy(self.trees[path])
        return tree, planner.canonical(tree) + b"\n", []

    def stream(self, url):
        self.stream_calls.append(url)
        if self.before_stream is not None:
            self.before_stream(url)
        response = Response(self.payloads[url])
        self.responses.append(response)
        return response, []


@pytest.fixture
def fixture_source(tmp_path, monkeypatch):
    monkeypatch.setattr(planner, "ROOT", tmp_path)
    monkeypatch.setattr(acquisition, "ROOT", tmp_path)
    fetch, _builder = planner.helpers()
    for name in (*planner.SOURCE_FILES, *acquisition.SOURCE_FILES):
        write(tmp_path / name, b"fixture frozen implementation\n")
    source = tmp_path / "artifacts/training/old-source"
    config = b"traj_map:\n  traj_scale_ratio: 1\n  pcd_scale_ratio: 1\n"
    trees, payloads, routes, rows = {}, {}, [], [{} for _ in range(221)]
    config_entries = []
    for color_index, (index, scene, partition, frame_count) in enumerate(acquisition.ROUTES):
        image_path = f"{scene}/astar_data/low_short/fixture-{index}"
        names = [f"20250101_175035_{i}" for i in range(frame_count)]
        row = {
            "image_path": image_path,
            "gpt_instruction": "Inspect the building at the end of this route.",
            "action": [9, 0],
            "index_list": [names[0], names[-1]],
            "pos": [[0, 0, 0], [3 * (frame_count - 1), 0, 0]],
            "yaw": [0, 0],
        }
        rows[index] = row
        prefix = "Image/" + image_path + "/"
        pose = [
            {
                "action": {
                    "imageid": i,
                    "pos": [3 * i, 0, 0],
                    "yaw": 0,
                    "type": "stop" if i == frame_count - 1 else "go straight",
                    "value": 0 if i == frame_count - 1 else 3,
                }
            }
            for i in range(frame_count)
        ] + [{"aim_landmark": [3 * (frame_count - 1), 0, 0]}]
        pose_raw = b"\n".join(planner.canonical(value) for value in pose)
        tree = [publisher_entry(prefix + "pose.jsonl", pose_raw)]
        payloads[fetch.dataset_url(prefix + "pose.jsonl")] = pose_raw
        for i, name in enumerate(names):
            buffer = io.BytesIO()
            with Image.new("RGB", (8, 8), (color_index * 70 + i, 20, 30)) as image:
                image.save(buffer, format="PNG")
            raw = buffer.getvalue()
            path = prefix + name + ".png"
            tree.append(publisher_entry(path, raw))
            payloads[fetch.dataset_url(path)] = raw
        trees["Image/" + image_path] = tree
        config_name = f"configs/{scene}.yaml"
        config_entries.append(publisher_entry(config_name, config, kind="blob"))
        payloads[
            f"https://raw.githubusercontent.com/{fetch.CODE_REPOSITORY}/{fetch.CODE_REVISION}/{config_name}"
        ] = config
        routes.append(
            {
                "annotation_index": index,
                "scene_id": scene,
                "partition": partition,
                "historical_training_route": False,
                "image_path": image_path,
                "annotation_record_sha256": hashlib.sha256(planner.canonical(row)).hexdigest(),
                "raw_root": acquisition.OUTPUT + "/raw",
                "episode_tree": None,
                "scene_config": None,
            }
        )
    annotation = write(source / "raw/Annotation/train.json", rows)
    monkeypatch.setattr(fetch, "ANNOTATION_BYTES", annotation["bytes"])
    monkeypatch.setattr(fetch, "ANNOTATION_SHA256", annotation["sha256"])
    card = write(source / "license/dataset/README.md", b"---\nlicense: apache-2.0\n---\n")
    card.update(repository=fetch.DATASET, repository_path="README.md")
    code_tree = write(
        source / "metadata/code-tree.json",
        {"sha": fetch.CODE_REVISION, "truncated": False, "tree": config_entries},
    )
    manifest = {
        "contract": "multidetect.openfly-navigation-pilot-v1",
        "dataset": {
            "repository": fetch.DATASET,
            "revision": fetch.REVISION,
            "split": "train",
            "license": "apache-2.0",
        },
        "code": {"revision": fetch.CODE_REVISION},
        "annotation": annotation,
        "episodes": [],
        "code_sources": [],
        "metadata_records": [code_tree],
        "license_sources": [card],
        "source_files": [],
        "seen_unseen_downloaded": False,
        "source_planned_not_executed": True,
    }
    manifest_path = source / "manifest.json"
    manifest_record = write(manifest_path, manifest)
    preflight = {
        "contract": planner.CONTRACT,
        "acquisition_root": acquisition.OUTPUT,
        "dataset_repository": fetch.DATASET,
        "dataset_revision": fetch.REVISION,
        "code_revision": fetch.CODE_REVISION,
        "source_manifest": manifest_record,
        "historical_train_scene": "env_airsim_18",
        "scene_assignment": {"env_airsim_18": "train"},
        "routes": routes,
        "source_files": [planner.binding(tmp_path / name) for name in planner.SOURCE_FILES],
    }
    preflight_binding = write(tmp_path / acquisition.PREFLIGHT_PLAN, preflight)
    source_bindings = sorted(
        planner.source_records(manifest) + [manifest_record, preflight_binding],
        key=lambda row: row["path"],
    )
    report = {
        "contract": planner.REPORT_CONTRACT,
        "native_exit_code": 0,
        "plan": preflight_binding,
        "sources_before": source_bindings,
        "sources_after": copy.deepcopy(source_bindings),
        "sources_unchanged": True,
    }
    write(tmp_path / acquisition.PREFLIGHT_REPORT, report)
    return SimpleNamespace(
        root=tmp_path,
        source=source,
        manifest=manifest,
        manifest_path=manifest_path,
        rows=rows,
        preflight=preflight,
        report=report,
        trees=trees,
        payloads=payloads,
        client=Client(trees, payloads),
        output=tmp_path / acquisition.OUTPUT,
    )


def saved_plan(source):
    value = acquisition.build_plan()
    path = source.root / "configs/training/new-download-plan.json"
    record = write(path, value)
    return path, record["sha256"], value


def test_actual_plan_binds_only_three_routes_and_no_training(fixture_source):
    source = fixture_source
    path, digest, value = saved_plan(source)
    admitted, before = acquisition.admit_plan(path, digest)
    assert admitted == value
    assert len(admitted["routes"]) == 3
    assert [
        (row["annotation_index"], row["required_frame_count"]) for row in admitted["routes"]
    ] == [(139, 23), (220, 17), (90, 42)]
    assert any(row["path"].endswith("new-download-plan.json") for row in before)
    assert all(item is False for item in admitted["claims"].values())
    assert not source.output.exists()
    assert source.client.tree_calls == [] and source.client.stream_calls == []


def test_complete_actual_rgb_pose_and_digest_validation(fixture_source):
    source = fixture_source
    path, digest, _value = saved_plan(source)
    receipt, record = acquisition.acquire(path, digest, client=source.client)
    assert receipt["native_exit_code"] == 0
    assert receipt["verified_rgb_count"] == 82
    assert receipt["verified_pose_count"] == 3
    assert receipt["verified_scene_config_count"] == 3
    assert receipt["sources_before"] == receipt["sources_after"]
    assert receipt["partial_files"] == []
    assert all(receipt[name] is False for name in acquisition.GATES)
    assert receipt["download_performed"] is True
    assert len(source.client.tree_calls) == 3 and len(source.client.stream_calls) == 88
    assert all(response.closed for response in source.client.responses)
    manifest = planner.strict_json((source.output / "manifest.json").read_bytes())
    assert [row["eligible_step_count"] for row in manifest["routes"]] == [23, 17, 42]
    assert (
        manifest["training_ready"] is False
        and manifest["landmark_truth_model_input_allowed"] is False
    )
    assert record == planner.binding(source.output / "receipt.json")
    with pytest.raises(FileExistsError):
        acquisition.acquire(path, digest, client=source.client)


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.update(download_authorized=False),
        lambda p: p["budget"].update(max_total_download_bytes=256 * 1024 * 1024),
        lambda p: p.update(output_directory="artifacts/training/old-source"),
        lambda p: p["routes"][0].update(annotation_index=140),
        lambda p: p["routes"][0].update(partition="dev-test"),
        lambda p: p["routes"][0].update(required_frame_count=True),
        lambda p: p["claims"].update(training_ready=True),
        lambda p: p.update(unreviewed=True),
        lambda p: p["sources"].pop(),
    ],
)
def test_plan_tampering_fails_before_network(fixture_source, change):
    path, _digest, value = saved_plan(fixture_source)
    change(value)
    record = write(path, value)
    with pytest.raises(ValueError):
        acquisition.acquire(path, record["sha256"], client=fixture_source.client)
    assert fixture_source.client.tree_calls == []
    assert not fixture_source.output.exists()


def test_explicit_sha_and_source_drift_fail_before_network(fixture_source):
    path, digest, _ = saved_plan(fixture_source)
    with pytest.raises(ValueError, match="SHA"):
        acquisition.acquire(path, "0" * 64, client=fixture_source.client)
    (fixture_source.root / acquisition.SOURCE_FILES[0]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="sources"):
        acquisition.acquire(path, digest, client=fixture_source.client)
    assert not fixture_source.output.exists()


@pytest.mark.parametrize(
    "error", ["missing_pose", "extra_frame", "wrong_name", "escape", "oversize", "bad_lfs"]
)
def test_all_publisher_metadata_validated_before_any_payload(fixture_source, error):
    source = fixture_source
    path, digest, _ = saved_plan(source)
    tree = source.client.trees[list(source.client.trees)[-1]]
    if error == "missing_pose":
        del tree[0]
    elif error == "extra_frame":
        extra = copy.deepcopy(tree[-1])
        extra["path"] = extra["path"].replace("_41.png", "_42.png")
        tree.append(extra)
    elif error == "wrong_name":
        tree[-1]["path"] = tree[-1]["path"].replace("175035", "175036")
    elif error == "escape":
        tree[0]["path"] = "Image/env_airsim_18/other/pose.jsonl"
    elif error == "oversize":
        tree[0]["size"] = acquisition.BUDGET["max_payload_file_bytes"] + 1
    else:
        tree[0]["lfs"] = {"size": True, "oid": "0" * 64}
    receipt, _binding = acquisition.acquire(path, digest, client=source.client)
    assert receipt["native_exit_code"] == 1
    assert source.client.stream_calls == []
    assert receipt["verified_rgb_count"] == 0
    assert not (source.output / "manifest.json").exists()
    assert not (source.output / "payload-plan.json").exists()
    assert (source.output / "receipt.json").is_file()


def test_total_budget_rejected_before_any_payload(fixture_source):
    source = fixture_source
    path, digest, _ = saved_plan(source)
    for tree in source.client.trees.values():
        for entry in tree:
            entry["size"] = 2 * 1024 * 1024
    receipt, _ = acquisition.acquire(path, digest, client=source.client)
    assert receipt["native_exit_code"] == 1
    assert "total download cap" in receipt["failure"]["reason"]
    assert source.client.stream_calls == []


def test_digest_failure_retains_partial_and_native_failure(fixture_source):
    source = fixture_source
    path, digest, _ = saved_plan(source)
    first = next(iter(source.client.payloads))
    original = source.client.payloads[first]
    source.client.payloads[first] = b"x" * len(original)
    receipt, _ = acquisition.acquire(path, digest, client=source.client)
    assert receipt["native_exit_code"] == 1
    assert len(receipt["partial_files"]) == 1
    assert receipt["verified_pose_count"] == receipt["verified_rgb_count"] == 0
    assert not (source.output / "manifest.json").exists()
    assert receipt["sources_unchanged"] is True
    assert all(receipt[key] is False for key in acquisition.GATES)


def test_all_trees_and_payload_plan_exist_before_download(fixture_source):
    source = fixture_source
    path, digest, _ = saved_plan(source)

    def assert_metadata_before(_url):
        assert len(source.client.tree_calls) == 3
        document = planner.strict_json((source.output / "payload-plan.json").read_bytes())
        assert len(document["downloads"]) == 88
        assert document["all_payload_metadata_verified_before_download"] is True

    source.client.before_stream = assert_metadata_before
    receipt, _ = acquisition.acquire(path, digest, client=source.client)
    assert receipt["native_exit_code"] == 0


def test_plan_drift_during_metadata_blocks_all_payloads(fixture_source):
    source = fixture_source
    path, digest, _ = saved_plan(source)
    source.client.after_tree = lambda _path: path.write_bytes(b"changed")
    receipt, _ = acquisition.acquire(path, digest, client=source.client)
    assert receipt["native_exit_code"] == 1
    assert receipt["status"] == "source_drift_failed"
    assert receipt["sources_unchanged"] is False
    assert receipt["acquisition_plan"]["sha256"] == digest
    assert source.client.stream_calls == []


def test_conflicting_original_source_binding_never_overwritten(fixture_source):
    source = fixture_source
    report = copy.deepcopy(source.report)
    report["sources_before"][0]["sha256"] = "0" * 64
    report["sources_after"] = copy.deepcopy(report["sources_before"])
    write(source.root / acquisition.PREFLIGHT_REPORT, report)
    with pytest.raises(ValueError, match="Conflicting"):
        acquisition.build_plan()


@pytest.mark.parametrize(
    "bad", ["../old", "artifacts/../old", "/outside", "artifacts//new", "artifacts\\new"]
)
def test_output_traversal_rejected(fixture_source, bad):
    with pytest.raises(ValueError):
        acquisition.guarded_path(bad, fresh=True)


def test_helper_does_not_mutate_sys_path(fixture_source):
    original = sys.path.copy()
    acquisition.helpers()
    assert sys.path == original


def test_tree_bytes_mismatch_is_not_metadata_evidence(fixture_source):
    source = fixture_source
    path, digest, _ = saved_plan(source)
    original_tree = source.client.tree

    def wrong_raw(name):
        tree, _raw, trail = original_tree(name)
        return tree, b"[]", trail

    source.client.tree = wrong_raw
    receipt, _ = acquisition.acquire(path, digest, client=source.client)
    assert receipt["native_exit_code"] == 1
    assert source.client.stream_calls == []
    assert receipt["metadata"][0]["bytes"] == 2


@pytest.mark.parametrize("kind", ["invalid_card", "truncated_tree", "unknown_config"])
def test_source_license_and_code_metadata_fail_closed(fixture_source, kind):
    source = fixture_source
    if kind == "invalid_card":
        path = source.source / "license/dataset/README.md"
        write(path, b"license: gated\n")
        with pytest.raises(ValueError):
            acquisition.build_plan()
        return
    tree_path = source.source / "metadata/code-tree.json"
    tree = planner.strict_json(tree_path.read_bytes())
    if kind == "truncated_tree":
        tree["truncated"] = True
    else:
        tree["tree"].pop()
    record = write(tree_path, tree)
    source.manifest["metadata_records"] = [record]
    manifest_record = write(source.manifest_path, source.manifest)
    source.preflight["source_manifest"] = manifest_record
    preflight_record = write(source.root / acquisition.PREFLIGHT_PLAN, source.preflight)
    source.report["plan"] = preflight_record
    source.report["sources_before"] = sorted(
        planner.source_records(source.manifest) + [manifest_record, preflight_record],
        key=lambda row: row["path"],
    )
    source.report["sources_after"] = copy.deepcopy(source.report["sources_before"])
    write(source.root / acquisition.PREFLIGHT_REPORT, source.report)
    path, digest, _ = saved_plan(source)
    receipt, _ = acquisition.acquire(path, digest, client=source.client)
    assert receipt["native_exit_code"] == 1
    assert source.client.stream_calls == []
