"""Small source/metadata fixtures, not actual82 decoding or training evidence.

The orchestration fixture uses the frozen typed contracts and geometry functions,
but stand-in Actor, source Session, and close-tracked image objects. No processor,
model, PNG decoder, large annotation, network, or GPU is run by these tests.
"""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from multidetect.autonomy import onboard_navigation_supervision as navigation
from scripts import build_onboard_openfly_navigation_dataset as builder
from scripts import build_onboard_openfly_original82_supervision as bridge
from scripts import fetch_openfly_navigation_pilot as fetch
from scripts import plan_openfly_multiscene_acquisition as planner

REPO = Path(__file__).resolve().parents[1]
SHA = "a" * 64


def declared(path, size=1, digest=SHA):
    return {"path": str(path), "bytes": size, "sha256": digest}


def chain_fixture():
    extraction = {
        "contract": "multidetect.openfly-original-embedded-png-evidence-v1",
        "native_exit_code": 0,
        "sources_unchanged": True,
        "sources_before": [declared("source.py")],
        "sources_after": [declared("source.py")],
        "all_82_original_images_verified": True,
        "all_source_shard_row_to_encoded_byte_bindings_verified": True,
        "transcoded_images": 0,
        "reserved_seen_unseen_read": False,
        "images": [],
    }
    plan = {"contract": "multidetect.openfly-embedded-png-extraction-plan-v1", "routes": []}
    geometry = {
        "contract": "multidetect.openfly-original82-geometry-visual-audit-v1",
        "native_exit_code": 0,
        "sources_unchanged": True,
        "sources_before": [declared("source.py")],
        "sources_after": [declared("source.py")],
        "original82_pose_successor_geometry_verified": True,
        "future_pose_or_label_sent_to_actor": False,
        "cross_scene_visual_edges": [],
        "visual_groups": [{"scene_id": "env_airsim_18", "frames": [{} for _ in range(86)]}],
    }
    for annotation_index, scene, partition, count in bridge.ROUTES:
        image_path = f"{scene}/astar_data/low_short/episode"
        shard = declared(f"{scene}.parquet", 100)
        publisher = {"digest": SHA, "bytes": 100, "digest_kind": "sha256-lfs"}
        route = {
            "annotation_index": annotation_index,
            "scene_id": scene,
            "development_partition": partition,
            "required_frame_count": count,
            "original_annotation_split": "train",
            "image_path": image_path,
            "shard_file": shard,
            "shard_publisher": publisher,
            "metadata_rows": [],
        }
        visuals = []
        for step in range(count):
            # Match the declared actual total without creating any real image bytes.
            size = 683429 if len(extraction["images"]) < 81 else 683482
            file = declared(f"images/{scene}/20250101_000000_{step}.png", size)
            route["metadata_rows"].append(
                {
                    "frame_index": step,
                    "image_id": f"20250101_000000_{step}",
                    "env_id": image_path.rsplit("/", 1)[0],
                    "traj_id": "episode",
                }
            )
            extraction["images"].append(
                {
                    "annotation_index": annotation_index,
                    "frame_index": step,
                    "parquet_row_index": step,
                    "image_id": f"20250101_000000_{step}",
                    "scene_id": scene,
                    "image_path": image_path,
                    "original_annotation_split": "train",
                    "source_shard": shard,
                    "publisher_shard": publisher,
                    "file": file,
                    "embedded_encoded_sha256": SHA,
                    "embedded_encoded_bytes": size,
                    "original_encoded_bytes_preserved": True,
                    "opaque_embedded_path_used_as_source": False,
                    "metadata_identity_matches_stage1": True,
                    "png": {
                        "width": 853,
                        "height": 480,
                        "mode": "RGB",
                        "encoding": "PNG",
                        "actual_Pillow_decode_succeeded": True,
                        "transcoded": False,
                    },
                }
            )
            visuals.append({"path": file["path"]})
        plan["routes"].append(route)
        geometry["visual_groups"].append({"scene_id": scene, "frames": visuals})
    return extraction, plan, geometry


def test_complete_declared_chain():
    bridge.validate_chain(*chain_fixture())


@pytest.mark.parametrize(
    "mutation",
    [
        "native_bool",
        "source_after",
        "transcoded_bool",
        "official_holdout_read",
        "future_truth",
        "missing_image",
        "reordered_image",
        "wrong_role",
        "wrong_scene",
        "wrong_split",
        "publisher_sha",
        "publisher_kind",
        "row_index_bool",
        "row_env",
        "row_traj",
        "row_image_id",
        "row_to_file_sha",
        "encoded_size",
        "encoded_float",
        "opaque_path",
        "transcoded_png",
        "non_rgb",
        "visual_same_basename_other_path",
        "missing_old_anchor",
        "duplicate_group",
        "cross_scene_edge",
        "wrong_total",
    ],
)
def test_declared_chain_rejects_mismatch(mutation):
    e, p, g = chain_fixture()
    image, route = e["images"][0], p["routes"][0]
    if mutation == "native_bool":
        e["native_exit_code"] = False
    elif mutation == "source_after":
        e["sources_after"][0]["sha256"] = "b" * 64
    elif mutation == "transcoded_bool":
        e["transcoded_images"] = False
    elif mutation == "official_holdout_read":
        e["reserved_seen_unseen_read"] = True
    elif mutation == "future_truth":
        g["future_pose_or_label_sent_to_actor"] = True
    elif mutation == "missing_image":
        e["images"].pop()
    elif mutation == "reordered_image":
        e["images"][0], e["images"][1] = e["images"][1], e["images"][0]
    elif mutation == "wrong_role":
        route["development_partition"] = "dev-test"
    elif mutation == "wrong_scene":
        image["scene_id"] = "env_airsim_18"
    elif mutation == "wrong_split":
        image["original_annotation_split"] = "test"
    elif mutation == "publisher_sha":
        route["shard_publisher"]["digest"] = "b" * 64
    elif mutation == "publisher_kind":
        route["shard_publisher"]["digest_kind"] = "sha1-git-blob"
    elif mutation == "row_index_bool":
        image["parquet_row_index"] = False
    elif mutation == "row_env":
        route["metadata_rows"][0]["env_id"] = "other-scene"
    elif mutation == "row_traj":
        route["metadata_rows"][0]["traj_id"] = "other-episode"
    elif mutation == "row_image_id":
        route["metadata_rows"][0]["image_id"] = "other-image"
    elif mutation == "row_to_file_sha":
        image["embedded_encoded_sha256"] = "b" * 64
    elif mutation == "encoded_size":
        image["embedded_encoded_bytes"] += 1
    elif mutation == "encoded_float":
        image["file"]["bytes"] = float(image["file"]["bytes"])
    elif mutation == "opaque_path":
        image["opaque_embedded_path_used_as_source"] = True
    elif mutation == "transcoded_png":
        image["png"]["transcoded"] = True
    elif mutation == "non_rgb":
        image["png"]["mode"] = "RGBA"
    elif mutation == "visual_same_basename_other_path":
        g["visual_groups"][1]["frames"][0]["path"] = "wrong/" + Path(image["file"]["path"]).name
    elif mutation == "missing_old_anchor":
        g["visual_groups"][0]["frames"].pop()
    elif mutation == "duplicate_group":
        g["visual_groups"].append(g["visual_groups"][0])
    elif mutation == "cross_scene_edge":
        g["cross_scene_visual_edges"] = [["env_airsim_26", "env_airsim_16"]]
    elif mutation == "wrong_total":
        image["file"]["bytes"] += 1
        image["embedded_encoded_bytes"] += 1
    with pytest.raises(ValueError):
        bridge.validate_chain(e, p, g)


@pytest.mark.parametrize(
    "payload",
    [
        '{"x": 1, "x": 2}',
        '{"x": NaN}',
        '{"x": Infinity}',
        '{"x": 1e999}',
        '{"x":',
    ],
)
def test_strict_json_rejects(payload):
    with pytest.raises(ValueError):
        bridge.strict_json(payload)


def test_exact_source_deduplicates_same_binding_but_not_conflicting(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "ROOT", tmp_path)
    a = declared("scripts/dependency.py")
    b = declared(tmp_path / "scripts/dependency.py")
    assert bridge.exact_source([a, b], "scripts/dependency.py") in (a, b)
    b["sha256"] = "b" * 64
    with pytest.raises(ValueError, match="Conflicting"):
        bridge.exact_source([a, b], "scripts/dependency.py")


def test_snapshot_hashes_each_admitted_file_once(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "ROOT", tmp_path)
    path = tmp_path / "small.txt"
    path.write_bytes(b"fixture")
    row = bridge.record(path)
    original, calls = bridge.record, []

    def counted(file):
        calls.append(file)
        return original(file)

    monkeypatch.setattr(bridge, "record", counted)
    assert len(bridge.snapshot([row, row, {**row, "path": "small.txt"}])) == 1
    assert len(calls) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("bytes", True),
        ("bytes", 0),
        ("bytes", 500 * 1024 * 1024),
        ("sha256", "A" * 64),
        ("path", 1),
    ],
)
def test_source_binding_types(monkeypatch, tmp_path, field, value):
    monkeypatch.setattr(bridge, "ROOT", tmp_path)
    file = tmp_path / "small.txt"
    file.write_bytes(b"fixture")
    row = bridge.record(file)
    row[field] = value
    with pytest.raises(ValueError):
        bridge.path_for(row)


def test_repository_escape_rejected_but_explicit_runtime_allowed(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    file = tmp_path / "runtime.py"
    file.write_bytes(b"fixture")
    monkeypatch.setattr(bridge, "ROOT", root)
    row = bridge.record(file)
    with pytest.raises(ValueError, match="escaped"):
        bridge.verify(row)
    assert bridge.verify(row, runtime=True) == file.resolve()


def visuals(scene, color, dhash):
    return {
        "scene_id": scene,
        "frames": [
            {
                "rgb_sha256": hashlib.sha256(scene.encode()).hexdigest(),
                "dhash64": dhash,
                "mean_rgb": [color] * 3,
            }
        ],
    }


def test_fixed_split_preserves_roles_and_rejects_visual_overlap():
    episodes = []
    for i, (_index, scene, part, _count) in enumerate(bridge.ROUTES):
        episodes.append(
            {
                "episode_id": scene,
                "scene_id": scene,
                "source_episode": scene,
                "declared_partition": part,
                "visual_identities": visuals(scene, i * 50, "0" * 16)["frames"],
            }
        )
    episodes.append(
        {
            "episode_id": "old18",
            "scene_id": "env_airsim_18",
            "source_episode": "old18",
            "declared_partition": "train",
            "visual_identities": visuals("old18", 200, "f" * 16)["frames"],
        }
    )
    split = bridge.fixed_partitions(episodes, planner)
    assert split["assignment"] == {ep["episode_id"]: ep["declared_partition"] for ep in episodes}
    episodes[1]["visual_identities"] = copy.deepcopy(episodes[0]["visual_identities"])
    with pytest.raises(ValueError, match="overlap"):
        bridge.fixed_partitions(episodes, planner)


@pytest.mark.parametrize("missing", [False, True])
def test_annotation_selection_closes_bounded_iterator(missing):
    closed, visited = [], []

    def iterate(_path):
        try:
            for index in range(222):
                visited.append(index)
                if not missing or index != 139:
                    yield index, {"fixture": index}
        finally:
            closed.append(True)

    api = SimpleNamespace(iter_annotation_records=iterate, validate_annotation=lambda _row: None)
    if missing:
        with pytest.raises(ValueError, match="missing"):
            bridge.select_annotations(Path("not-read"), api)
    else:
        selected, count = bridge.select_annotations(Path("not-read"), api)
        assert set(selected) == {90, 139, 220}
        assert count == 221 and visited[-1] == 220
    assert closed == [True]


@pytest.fixture
def orchestration(monkeypatch, tmp_path):
    """Tiny files + real source math/types, with no image decoding or source proof claim."""
    monkeypatch.setattr(bridge, "ROOT", tmp_path)
    source_rows = []

    def write(name, raw):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        row = bridge.relative(bridge.record(path))
        source_rows.append(row)
        return row

    entry = write("scripts/build_onboard_openfly_original82_supervision.py", b"fixture entrypoint")
    write("tests/test_build_onboard_openfly_original82_supervision.py", b"fixture tests")
    monkeypatch.setattr(bridge, "__file__", str(tmp_path / entry["path"]))
    license_row = write("license/dataset/README.md", b"license: apache-2.0\n")
    routes, images, geometry_routes, groups = [], [], [], []
    annotations = [{"unused_fixture": i} for i in range(222)]
    for number, (index, scene, partition, count) in enumerate(bridge.ROUTES):
        image_path = f"{scene}/astar_data/low_short/episode"
        actions = [
            {
                "imageid": i,
                "pos": [float(i), 0.0, 0.0],
                "yaw": 0.0,
                "type": "go straight" if i < count - 1 else "stop",
                "value": 1 if i < count - 1 else 0,
            }
            for i in range(count)
        ]
        original = {
            "image_path": image_path,
            "gpt_instruction": f"原文 instruction {scene}\n",
            "action": [1] * (count - 1) + [0],
            "index_list": [f"20250101_000000_{i}" for i in range(count)],
            "pos": [a["pos"] for a in actions],
            "yaw": [a["yaw"] for a in actions],
        }
        annotations[index] = original
        pose_raw = b"\n".join(bridge.canonical({"action": a}) for a in actions)
        pose_raw += b'\n{"aim_landmark": {"offline_future_fixture": true}}'
        pose = write(f"raw/Image/{image_path}/pose.jsonl", pose_raw)
        config = write(
            f"raw/configs/{scene}.yaml",
            b'{"traj_map": {"traj_scale_ratio":1, "pcd_scale_ratio":1}}',
        )

        def publisher(row):
            raw = (tmp_path / row["path"]).read_bytes()
            return {
                "bytes": len(raw),
                "digest_kind": "sha1-git-blob",
                "digest": hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest(),
            }

        route = {
            "annotation_index": index,
            "scene_id": scene,
            "development_partition": partition,
            "image_path": image_path,
            "original_annotation_record_sha256": hashlib.sha256(
                planner.canonical(original)
            ).hexdigest(),
            "pose_publisher": publisher(pose),
            "config_publisher": publisher(config),
            "metadata_rows": [
                {
                    "frame_index": a["imageid"],
                    "pos": a["pos"],
                    "yaw": a["yaw"],
                    "action_type": a["type"],
                    "action_value": a["value"],
                }
                for a in actions
            ],
        }
        routes.append(route)
        for i in range(count):
            image = write(f"images/{scene}/20250101_000000_{i}.png", b"not-a-PNG-fixture")
            images.append({"annotation_index": index, "file": image})
        metrics = [builder.derive_primitive(actions, i) for i in range(count)]
        geometry_routes.append(
            {
                "annotation_index": index,
                "targets_and_geometry_checks": [{"target": t, "check": c} for t, c in metrics],
            }
        )
        groups.append(visuals(scene, number * 50, "0" * 16))
    groups.append(visuals("env_airsim_18", 200, "f" * 16))
    annotation = write("raw/Annotation/train.json", bridge.canonical(annotations))
    controls = {
        name: write(f"controls/{name}.json", b'{"fixture_only":true}') for name in bridge.CONTROLS
    }
    runtime = [write(f"runtime/module{i}.py", f"fixture runtime {i}".encode()) for i in range(6)]
    ctx = SimpleNamespace(
        plan={
            "routes": routes,
            "runtime": {"versions": {"Pillow": "fixture", "PyYAML": "fixture"}},
            "original_train_annotation": annotation,
            "dataset_repository": fetch.DATASET,
            "dataset_revision": fetch.REVISION,
        },
        geometry={
            "sources_before": source_rows.copy(),
            "routes": geometry_routes,
            "visual_groups": groups,
        },
        extraction={"images": images},
        manifest={
            "code": {"revision": fetch.CODE_REVISION},
            "code_sources": [],
            "license_sources": [license_row],
        },
        controls=controls,
        sources=source_rows.copy(),
        runtime=runtime,
    )
    state = SimpleNamespace(images=[], seen=[], sessions=[], mode="normal")

    class Session:
        def __init__(self, _root):
            self.verified = False
            state.sessions.append(self)

        def __enter__(self):
            return self

        def prime(self, samples):
            assert len(samples) == 82
            assert all(isinstance(s, navigation.NavigationSupervisionSample) for s in samples)

        def __exit__(self, _type, _error, _traceback):
            assert all(image.closed for image in state.images)
            if state.mode == "exit_error":
                raise ValueError("fixture final source SHA failed")
            self.verified = state.mode != "unverified"
            return state.mode == "suppressed_actor_error"

    class FixtureActor:
        @staticmethod
        def from_supervision_sample(sample, _root, *, session):
            assert session.verified is False
            state.seen.append(sample)
            if state.mode in {"actor_error", "suppressed_actor_error"} and len(state.seen) == 4:
                raise ValueError("fixture Actor failed")
            if state.mode == "source_change" and len(state.seen) == 4:
                (tmp_path / license_row["path"]).write_bytes(b"changed")
            return SimpleNamespace(sample=sample, identity_sha256=sample.canonical_hash)

    class FixtureImage:
        def __init__(self):
            self.closed = False
            state.images.append(self)

        def close(self):
            self.closed = True

    def messages(actor):
        return [
            {"role": "system", "content": [{"type": "text", "text": "fixture"}]},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": actor.sample.prompt.text_original},
                    *[{"type": "image", "image": FixtureImage()} for _ in actor.sample.frames],
                ],
            },
        ]

    fake_fetch = SimpleNamespace(
        ANNOTATION_BYTES=annotation["bytes"],
        ANNOTATION_SHA256=annotation["sha256"],
        DATASET=fetch.DATASET,
        REVISION=fetch.REVISION,
        CODE_REVISION=fetch.CODE_REVISION,
        iter_annotation_records=fetch.iter_annotation_records,
        validate_annotation=fetch.validate_annotation,
        parse_pose=fetch.parse_pose,
        align_episode=fetch.align_episode,
    )
    fake_navigation = SimpleNamespace(
        NavigationSupervisionSample=navigation.NavigationSupervisionSample,
        validate_navigation_episode_samples=navigation.validate_navigation_episode_samples,
        serialize_navigation_supervision_target=lambda sample, _root, **_kwargs: bridge.canonical(
            {
                "kind": sample.planned_action.kind,
                "translation_m": sample.planned_action.translation_m,
                "yaw_delta_rad": sample.planned_action.yaw_delta_rad,
            }
        ),
    )
    api = SimpleNamespace(
        b=builder,
        f=fake_fetch,
        p=planner,
        n=fake_navigation,
        Session=Session,
        a=SimpleNamespace(
            NavigationActorInput=FixtureActor, serialize_navigation_actor_messages=messages
        ),
        yaml=SimpleNamespace(safe_load=json.loads),
        runtime_modules=[SimpleNamespace(__file__=str(tmp_path / r["path"])) for r in runtime],
    )
    monkeypatch.setattr(bridge, "admit_inputs", lambda: ctx)
    monkeypatch.setattr(bridge, "apis", lambda: api)
    monkeypatch.setattr(bridge.importlib.metadata, "version", lambda _name: "fixture")
    return SimpleNamespace(
        ctx=ctx,
        api=api,
        state=state,
        root=tmp_path,
        output=tmp_path / "artifacts/training/onboard-openfly-original82-supervision-fixture",
        entry_sha=entry["sha256"],
    )


def build_fixture(fixture):
    return bridge.build_bridge(fixture.output, entrypoint_sha256=fixture.entry_sha)


def test_orchestration_publishes_only_after_complete_verified_exit(orchestration):
    report = build_fixture(orchestration)
    assert report["sample_count"] == 82
    assert report["partition_counts"] == {"train": 23, "dev-validation": 17, "dev-test": 42}
    assert report["annotation_records_streamed_until_selections"] == 221
    assert report["source_validation_session_verified"] is True
    assert report["source_bindings_before"] == report["source_bindings_after"]
    assert report["runtime_bindings_before"] == report["runtime_bindings_after"]
    assert all(report[gate] is False for gate in bridge.FALSE_CLAIMS)
    assert len(report["sample_actor_target_bindings"]) == 82
    assert all(image.closed for image in orchestration.state.images)
    assert orchestration.state.sessions[0].verified is True
    rows = [
        navigation.NavigationSupervisionSample.from_json(line)
        for line in (orchestration.output / "samples.jsonl").read_bytes().splitlines()
    ]
    navigation.validate_navigation_episode_samples(tuple(rows))
    assert all(s.source.original_split == "train" and not s.training_ready for s in rows)
    assert Counter(s.planned_action.kind for s in rows) == {"translate": 79, "stop": 3}
    for sample, actor_binding in zip(rows, report["sample_actor_target_bindings"], strict=True):
        assert actor_binding["ordered_frame_steps"] == list(
            range(max(0, sample.step_index - 1), sample.step_index + 1)
        )
        assert sample.alignment.dt_s is None
        assert sample.frames[-1].source_stamp is None
        assert sample.planned_action.raw_source_pointer.record_index == sample.step_index
        assert sample.source.review_receipt is None
        assert "env_airsim_18" != sample.scene_id
    stop = next(s for s in rows if s.planned_action.kind == "stop")
    assert stop.planned_action.translation_m is None
    assert stop.planned_action.unit_verification_sha256 is None
    assert stop.alignment.next_step is None


@pytest.mark.parametrize(
    "mode",
    [
        "exit_error",
        "unverified",
        "actor_error",
        "suppressed_actor_error",
        "source_change",
    ],
)
def test_failed_final_exit_or_partial_body_never_publishes(orchestration, mode):
    orchestration.state.mode = mode
    with pytest.raises(ValueError):
        build_fixture(orchestration)
    assert not (orchestration.output / "samples.jsonl").exists()
    assert not (orchestration.output / "report.json").exists()
    failure = bridge.strict_json((orchestration.output / "failure.json").read_bytes())
    assert failure["native_exit_code"] == 1 and failure["partial_outputs_preserved"] is True
    assert all(failure[gate] is False for gate in bridge.FALSE_CLAIMS)
    assert all(image.closed for image in orchestration.state.images)


def test_fresh_directory_only_no_overwrite(orchestration):
    report = build_fixture(orchestration)
    before = (orchestration.output / "report.json").read_bytes()
    with pytest.raises(ValueError, match="fresh"):
        build_fixture(orchestration)
    assert (orchestration.output / "report.json").read_bytes() == before
    assert report["native_exit_code"] == 0


def test_entrypoint_admission_precedes_outputs(orchestration):
    with pytest.raises(ValueError, match="entrypoint"):
        bridge.build_bridge(orchestration.output, entrypoint_sha256="b" * 64)
    assert not orchestration.output.exists()


def test_imported_runtime_path_mismatch_rejected_before_output(orchestration):
    wrong = orchestration.root / "same_version_wrong_location.py"
    wrong.write_bytes(b"fixture runtime 0")
    orchestration.api.runtime_modules[0].__file__ = str(wrong)
    with pytest.raises(ValueError, match="entrypoints"):
        build_fixture(orchestration)
    assert not orchestration.output.exists()


@pytest.mark.parametrize(
    "mutation", ["annotation", "scale", "publisher", "geometry", "parquet_pose"]
)
def test_preparation_gates_reject_without_output(orchestration, mutation):
    ctx = orchestration.ctx
    if mutation == "annotation":
        ctx.plan["routes"][0]["original_annotation_record_sha256"] = "b" * 64
    elif mutation == "scale":
        orchestration.api.yaml.safe_load = lambda _raw: {
            "traj_map": {"traj_scale_ratio": 2, "pcd_scale_ratio": 1}
        }
    elif mutation == "publisher":
        ctx.plan["routes"][0]["pose_publisher"]["digest"] = "b" * 40
    elif mutation == "geometry":
        ctx.geometry["routes"][0]["targets_and_geometry_checks"][0]["target"]["translation_m"][
            0
        ] = 2.0
    elif mutation == "parquet_pose":
        ctx.plan["routes"][0]["metadata_rows"][0]["yaw"] = 1.0
    with pytest.raises(ValueError):
        build_fixture(orchestration)
    assert not orchestration.output.exists()


@pytest.mark.parametrize("kind,body", [("go up", [0.0, 0.0, -3.0]), ("go down", [0.0, 0.0, 3.0])])
def test_typed_vertical_labels_preserved_not_relabelled(orchestration, kind, body):
    report = build_fixture(orchestration)
    sample = orchestration.state.seen[0].to_mapping()
    action = {"imageid": 0, "pos": [0.0, 0.0, 0.0], "yaw": 0.0, "type": kind, "value": 3}
    sample["planned_action"].update(raw=action, translation_m=body)
    typed = navigation.NavigationSupervisionSample.from_mapping(sample)
    assert typed.planned_action.translation_m == tuple(body)
    assert report["compatible_with_legacy86_training_admission"] is False
    assert not typed.training_ready


def test_actual_sealed_metadata_chain_only_if_present():
    names = ("extraction", "plan", "geometry")
    files = [REPO / bridge.CONTROLS[name][0] for name in names]
    if not all(path.is_file() for path in files):
        pytest.skip("Optional runtime metadata is absent from a clean clone")
    documents = []
    for name, path in zip(names, files, strict=True):
        raw = path.read_bytes()
        assert len(raw) < 2 * 1024 * 1024
        assert hashlib.sha256(raw).hexdigest() == bridge.CONTROLS[name][1]
        documents.append(bridge.strict_json(raw))
    # Declarations only: no source/image hashes, PNG decode, Parquet, or 405 MB read.
    bridge.validate_chain(*documents)


def test_isolated_import_and_help_are_lightweight():
    path = REPO / "scripts/build_onboard_openfly_original82_supervision.py"
    code = (
        "import runpy,sys; runpy.run_path(sys.argv[1], run_name='bridge_import'); "
        "assert not any(n.split('.')[0] in {'torch','transformers','PIL','numpy','pyarrow','peft'} "
        "for n in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", code, str(path)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    help_result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", str(path), "--help"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert help_result.returncode == 0 and "--entrypoint-sha256" in help_result.stdout
