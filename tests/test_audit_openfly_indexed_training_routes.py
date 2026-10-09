"""Small CPU source fixtures, not actual122 admission or model evidence."""

import copy
import hashlib
import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from multidetect.autonomy import onboard_navigation_actor as actor
from multidetect.autonomy import onboard_navigation_supervision as navigation
from scripts import audit_openfly_indexed_training_routes as audit
from scripts import audit_openfly_multiscene_navigation_geometry as geometry
from scripts import build_onboard_openfly_navigation_dataset as builder
from scripts import build_onboard_openfly_original82_supervision as bridge
from scripts import build_openfly_bounded_annotation_index as indexer
from scripts import extract_openfly_parquet_images as extractor
from scripts import fetch_and_audit_openfly_parquet as acquisition
from scripts import fetch_openfly_navigation_pilot as fetch
from scripts.onboard_navigation_closed_sample_encoding import (
    ClosedNavigationSourceValidationSession,
)


def png(width=3, height=2):
    Image = pytest.importorskip("PIL.Image")
    stream = io.BytesIO()
    with Image.new("RGB", (width, height), (21, 42, 63)) as image:
        image.save(stream, format="PNG")
    return stream.getvalue()


def actions(count):
    return [
        {
            "imageid": i,
            "pos": [float(i * 3), 0.0, 0.0],
            "yaw": 0.0,
            "type": "stop" if i == count - 1 else "go straight",
            "value": 0 if i == count - 1 else 3,
        }
        for i in range(count)
    ]


def original(image_path, raw_actions):
    return {
        "image_path": image_path,
        "gpt_instruction": "Go to the blue building.",
        "action": [1] * (len(raw_actions) - 1) + [0],
        "index_list": [f"20250101_123000_{i}" for i in range(len(raw_actions))],
        "pos": [a["pos"] for a in raw_actions],
        "yaw": [a["yaw"] for a in raw_actions],
    }


def metadata(image_path, raw_actions):
    return [
        {
            "env_id": str(Path(image_path).parent).replace("\\", "/"),
            "traj_id": Path(image_path).name,
            "image_id": f"20250101_123000_{i}",
            "frame_index": i,
            "pos": a["pos"],
            "yaw": a["yaw"],
            "action_type": a["type"],
            "action_value": a["value"],
        }
        for i, a in enumerate(raw_actions)
    ]


@pytest.fixture
def fixture_run(tmp_path, monkeypatch):
    """Actual tiny PyArrow/Pillow/core geometry/Actor; admission boundary stand-ins."""
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    monkeypatch.setattr(audit, "forbidden_imports", lambda: [])
    routes, payloads, selected = [], [], {}
    for position, count in audit.ROUTES:
        image_path = f"env_airsim_26/astar_data/low_short/fixture-{position}"
        raw_actions = actions(count)
        selected[position] = original(image_path, raw_actions)
        route = {
            "annotation_index": position,
            "scene_id": "env_airsim_26",
            "image_path": image_path,
            "candidate_frame_count_from_terminal_id": count,
            "annotation_record_sha256": hashlib.sha256(
                indexer.canonical(selected[position])
            ).hexdigest(),
        }
        routes.append(route)
        pose_raw = b"\n".join(audit.canonical({"action": a}) for a in raw_actions)
        pose_raw += b'\n{"aim_landmark": [999.0, 888.0, 777.0]}\n'
        pose = audit.create_bytes(tmp_path / f"inputs/{position}/pose.jsonl", pose_raw)
        rows = [
            {**row, "image": {"bytes": png(3 + position % 2, 2), "path": "opaque-not-used"}}
            for row in metadata(image_path, raw_actions)
        ]
        shard_path = tmp_path / f"inputs/{position}/data.parquet"
        pq.write_table(pa.Table.from_pylist(rows, schema=acquisition.expected_schema()), shard_path)
        shard = audit.binding(shard_path)
        for role, file in (("shard", shard), ("pose", pose)):
            payloads.append(
                {
                    "annotation_index": position,
                    "role": role,
                    "file": file,
                    "repository_path": role + "/" + image_path,
                    "bytes": file["bytes"],
                    "digest_kind": "sha256-lfs" if role == "shard" else "sha1-git-blob",
                    "digest": file["sha256"]
                    if role == "shard"
                    else hashlib.sha1(
                        b"blob " + str(len(pose_raw)).encode() + b"\0" + pose_raw
                    ).hexdigest(),
                }
            )
    config = audit.create_bytes(
        tmp_path / "config.yaml", b"traj_map:\n  pcd_scale_ratio: 1\n  traj_scale_ratio: 1\n"
    )
    license_row = audit.create_bytes(tmp_path / "license.md", b"---\nlicense: apache-2.0\n---\n")
    source = audit.create_bytes(tmp_path / "source.txt", b"fixture source, not actual data")
    annotation = audit.create_bytes(
        tmp_path / "historical-not-read.json", b"declared annotation fixture"
    )
    plan = {
        "schema_version": 1,
        "contract": audit.PLAN_CONTRACT,
        "output_directory": audit.OUTPUT,
        "sources": [source],
        "payloads": payloads,
        "routes": routes,
        "config": config,
        "license": license_row,
        "runtime": {"fixture": True},
        "annotation_declared_not_rehashed": annotation,
        "annotation_stat": audit.annotation_stat(annotation["path"]),
        "annotation_prefix": {"bytes": audit.PREFIX_BYTES, "sha256": audit.PREFIX_SHA},
        "dataset_repository": fetch.DATASET,
        "dataset_revision": fetch.REVISION,
        "generation_code": [],
    }

    def fixture_metadata(route, records, record):
        # The production function is frozen audit_metadata. Fixture injection
        # supplies its output to exercise the NEW orchestration without patching
        # the frozen planner's repository root or reading actual acquisitions.
        pose = next(r["file"] for r in records if r["role"] == "pose")
        parsed, _ = fetch.parse_pose(audit.verify(pose).read_bytes())
        shard = next(r["file"] for r in records if r["role"] == "shard")
        reader, schema = acquisition.checked_parquet(audit.verify(shard), len(parsed))
        try:
            rows = reader.read(columns=list(acquisition.METADATA_COLUMNS)).to_pylist()
        finally:
            reader.close()
        fetch.align_episode(
            record,
            parsed,
            [(i, {"path": "Image/" + row["image_id"] + ".png"}) for i, row in enumerate(rows)],
        )
        return {"schema_metadata": schema, "metadata_rows": rows}

    api = SimpleNamespace(
        acq=SimpleNamespace(
            checked_parquet=acquisition.checked_parquet,
            METADATA_COLUMNS=acquisition.METADATA_COLUMNS,
            audit_metadata=fixture_metadata,
        ),
        f=fetch,
        b=builder,
        g=geometry,
        e=extractor,
        bridge=bridge,
        n=navigation,
        a=actor,
        Session=ClosedNavigationSourceValidationSession,
    )
    monkeypatch.setattr(audit, "build_plan", lambda: copy.deepcopy(plan))
    monkeypatch.setattr(audit, "apis", lambda _sources: api)
    monkeypatch.setattr(audit, "runtime_information", lambda: copy.deepcopy(plan["runtime"]))
    monkeypatch.setattr(
        audit, "select_annotations", lambda _plan, _api: (copy.deepcopy(selected), 6)
    )
    monkeypatch.setattr(
        audit, "verify_prefix", lambda _plan: copy.deepcopy(plan["annotation_prefix"])
    )
    plan_file = audit.create_json(tmp_path / "plan.json", plan)
    return SimpleNamespace(
        plan=plan,
        plan_file=plan_file,
        api=api,
        selected=selected,
        out=tmp_path / audit.OUTPUT,
        source=source,
    )


def execute(fixture):
    return audit.audit(fixture.plan_file["path"], fixture.plan_file["sha256"])


def test_complete_small_orchestration_same_scene_separate_episodes(fixture_run):
    report = execute(fixture_run)
    assert report["sample_count"] == 122
    assert report["episode_count"] == 6
    assert report["successor_count"] == 116
    assert report["terminal_stop_count"] == 6
    assert report["source_validation_session_verified"] is True
    assert report["partition_counts"] == {"train": 122}
    assert report["source_bindings_before"] == report["source_bindings_after"]
    assert all(report[key] is False for key in audit.FALSE_CLAIMS)
    samples = [
        navigation.NavigationSupervisionSample.from_mapping(audit.strict_json(line))
        for line in audit.verify(report["dataset"]).read_bytes().splitlines()
    ]
    assert len({s.scene_id for s in samples}) == 1
    assert len({s.episode_id for s in samples}) == 6
    assert all(s.frames[-1].step_index == s.step_index for s in samples)
    assert all(
        all(f.episode_id == s.episode_id and f.step_index <= s.step_index for f in s.frames)
        for s in samples
    )
    assert {f.width for s in samples for f in s.frames} == {3, 4}
    assert {f.height for s in samples for f in s.frames} == {2}
    selection = audit.read_bound(report["annotation_selection"])
    assert selection == {str(k): v for k, v in fixture_run.selected.items()}
    assert [s.prompt.source_pointer.json_pointer for s in samples if s.step_index == 0] == [
        f"/{position}/gpt_instruction" for position, _ in audit.ROUTES
    ]
    assert not (fixture_run.out / "failure.json").exists()
    assert report["pose_future_truth_or_label_in_actor_input"] is False
    with pytest.raises(ValueError, match="create-only"):
        execute(fixture_run)


def test_actor_truth_free_serialized_messages(fixture_run):
    report = execute(fixture_run)
    sample = navigation.NavigationSupervisionSample.from_mapping(
        audit.strict_json(audit.verify(report["dataset"]).read_bytes().splitlines()[1])
    )
    projected = actor.NavigationActorInput.from_supervision_sample(sample, audit.ROOT)
    messages = actor.serialize_navigation_actor_messages(projected)
    try:
        assert len(messages) == 2 and [m["role"] for m in messages] == ["system", "user"]
        texts = [item["text"] for item in messages[1]["content"] if item["type"] == "text"]
        assert sample.prompt.text_original in texts
        assert "aim_landmark" not in str(texts) and "position_xyz" not in str(texts)
        assert "999.0" not in str(texts) and "translation_m" not in str(texts)
        assert [f.sequence for f in projected.frames] == [0, 1]
    finally:
        for message in messages:
            for item in message["content"]:
                if item["type"] == "image":
                    item["image"].close()


@pytest.mark.parametrize(
    "mode",
    ["body", "exit", "suppressed", "unverified", "source_drift", "runtime_drift", "prefix_drift"],
)
def test_failure_never_publishes_samples_or_success(fixture_run, monkeypatch, mode):
    api = fixture_run.api
    if mode in {"body", "suppressed"}:

        def fail(*_args, **_kwargs):
            raise ValueError("fixture actor body failure")

        monkeypatch.setattr(api.a.NavigationActorInput, "from_supervision_sample", fail)
    if mode in {"exit", "suppressed", "unverified"}:

        class Session(ClosedNavigationSourceValidationSession):
            def __exit__(self, exc_type, exc, tb):
                if mode == "suppressed":
                    self.verified = True
                    return True
                result = super().__exit__(exc_type, exc, tb)
                if mode == "exit":
                    raise ValueError("fixture exit failed")
                self.verified = False
                return result

        api.Session = Session
    if mode == "source_drift":
        prior = audit.verify_actors

        def drift(samples, api):
            result = prior(samples, api)
            audit.local(fixture_run.source["path"]).write_bytes(b"drifted source")
            return result

        monkeypatch.setattr(audit, "verify_actors", drift)
    if mode == "runtime_drift":
        calls = []

        def runtime():
            calls.append(1)
            return {"fixture": len(calls) == 1}

        monkeypatch.setattr(audit, "runtime_information", runtime)
    if mode == "prefix_drift":
        calls = []

        def prefix(_plan):
            calls.append(1)
            if len(calls) > 1:
                raise ValueError("fixture final prefix changed")
            return fixture_run.plan["annotation_prefix"]

        monkeypatch.setattr(audit, "verify_prefix", prefix)
    with pytest.raises(ValueError):
        execute(fixture_run)
    assert not (fixture_run.out / "samples.jsonl").exists()
    assert not (fixture_run.out / "report.json").exists()
    failure = audit.strict_json((fixture_run.out / "failure.json").read_bytes())
    assert failure["native_exit_code"] == 1 and failure["partial_outputs_preserved"] is True
    assert failure["images_published_before_failure"] == 122
    assert all(failure[key] is False for key in audit.FALSE_CLAIMS)


def test_later_route_failure_preserves_prior_bytes(fixture_run, monkeypatch):
    previous = fixture_run.api.acq.audit_metadata

    def fail(route, records, record):
        if route["annotation_index"] == 150:
            raise ValueError("fixture second episode failure")
        return previous(route, records, record)

    fixture_run.api.acq.audit_metadata = fail
    with pytest.raises(ValueError, match="second episode"):
        execute(fixture_run)
    failure = audit.strict_json((fixture_run.out / "failure.json").read_bytes())
    assert failure["images_published_before_failure"] == 15
    assert len(list((fixture_run.out / "images").rglob("*.png"))) == 15
    assert not (fixture_run.out / "samples.jsonl").exists()


@pytest.mark.parametrize(
    "image",
    [
        None,
        {"path": "publisher.png", "bytes": None},
        {"path": "publisher.png"},
        {"path": "opaque", "bytes": b"bad PNG"},
    ],
)
def test_null_path_only_invalid_png_rejected(tmp_path, monkeypatch, image):
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    source = audit.create_bytes(tmp_path / "dummy.parquet", b"fixture only")
    row = {"frame_index": 0, "image_id": "20250101_123000_0", "image": image}
    closed = []

    class Reader:
        def iter_batches(self, **_kwargs):
            yield SimpleNamespace(num_rows=1, to_pylist=lambda: [row])

        def close(self):
            closed.append(True)

    api = SimpleNamespace(
        acq=SimpleNamespace(
            checked_parquet=lambda *_: (Reader(), {}), METADATA_COLUMNS=("frame_index", "image_id")
        ),
        e=extractor,
    )
    route = {"required_frame_count": 1, "shard_file": source}
    metadata_row = {
        "schema_metadata": {},
        "metadata_rows": [{"frame_index": 0, "image_id": row["image_id"]}],
    }
    with pytest.raises(ValueError):
        audit.extract_images(route, metadata_row, tmp_path / "out", api, [])
    assert closed == [True]
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("mutation", ["nan", "wrong_displacement", "unknown", "mismatched_row"])
def test_metric_derivation_no_repair(mutation):
    raw = actions(2)
    rows = metadata("env_airsim_26/astar_data/low_short/tiny", raw)
    if mutation == "nan":
        raw[1]["pos"][0] = float("nan")
    elif mutation == "wrong_displacement":
        raw[1]["pos"][0] = 2.0
    elif mutation == "unknown":
        raw[0]["type"] = "unsupported_original_primitive"
        rows[0]["action_type"] = raw[0]["type"]
    else:
        rows[0]["action_value"] = 7
    untouched = copy.deepcopy(raw)
    with pytest.raises(ValueError):
        geometry.metric_targets(raw, rows, builder.derive_primitive)
    assert raw[0] == untouched[0]


def test_actual_small_acquisition_declarations_without_payload_reads():
    # This checks only the three sealed small control documents, not actual122.
    if not (audit.ROOT / audit.REPORT).exists():
        pytest.skip("Runtime acquisition artifacts are absent in a clean clone")
    report, plan, index = [
        audit.read_bound(audit.pin(path))
        for path in (audit.REPORT, audit.ACQUISITION_PLAN, audit.INDEX)
    ]
    audit.validate_acquisition(report, plan, index)
    changed = copy.deepcopy(report)
    changed["routes"][1]["annotation_index"] = 143
    with pytest.raises(ValueError):
        audit.validate_acquisition(changed, plan, index)


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e999}'])
def test_strict_json(raw):
    with pytest.raises(ValueError):
        audit.strict_json(raw)


def test_unsigned_big_integer_not_float_roundtrip():
    value = {"raw_uint64": 18446744073709551615}
    assert audit.strict_json(audit.canonical(value)) == value


def test_binding_bool_and_escape_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    row = audit.create_bytes(tmp_path / "tiny.txt", b"x")
    with pytest.raises(ValueError):
        audit.verify({**row, "bytes": True})
    with pytest.raises(ValueError, match="escapes"):
        audit.local(tmp_path.parent, exists=True)
    with pytest.raises(FileExistsError):
        audit.create_bytes(tmp_path / "tiny.txt", b"replacement")


def test_wrong_plan_sha_precedes_import_output(fixture_run, monkeypatch):
    def fail(_sources):
        raise AssertionError("Must not import CPU helpers")

    monkeypatch.setattr(audit, "apis", fail)
    with pytest.raises(ValueError, match="plan SHA"):
        audit.audit(fixture_run.plan_file["path"], "0" * 64)
    assert not fixture_run.out.exists()


def test_runtime_actual_module_path_changes_fail(monkeypatch):
    calls = []
    monkeypatch.setattr(
        audit.importlib, "import_module", lambda name: SimpleNamespace(__file__="/changed/" + name)
    )
    monkeypatch.setattr(audit.importlib.metadata, "version", lambda _name: "fixture")
    monkeypatch.setattr(
        audit,
        "binding",
        lambda path, runtime=False: (
            calls.append(path) or {"path": path, "bytes": 1, "sha256": "0" * 64}
        ),
    )
    result = audit.runtime_information()
    assert len(calls) == 10
    assert set(result["modules"]) == set(audit.RUNTIME_MODULES)
    assert all(row["path"].startswith("/changed/") for row in result["modules"].values())


def test_prefix_reader_does_not_read_unadmitted_suffix(tmp_path, monkeypatch):
    # Reuse the unchanged parser on a bounded adapter, never fake EOF at cap.
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    selected = original("env_airsim_26/astar_data/low_short/tiny", actions(2))
    selected["pos"][0][0] = 18446744073709551615
    raw = audit.canonical([selected]) + b" " * 70000
    path = tmp_path / "annotation.json"
    path.write_bytes(raw + b"UNREAD_SUFFIX")
    cap = len(raw)
    monkeypatch.setattr(audit, "PREFIX_BYTES", cap)
    monkeypatch.setattr(audit, "PREFIX_SHA", hashlib.sha256(raw).hexdigest())

    class BudgetPath:
        def __init__(self, path, budget, source_size):
            self.path, self.limit = Path(path), budget.max_bytes
            self.raw = SimpleNamespace(bytes_read=0, sha256=hashlib.sha256(), close=lambda: None)

        def open(self, mode, *, encoding):
            owner = self

            class Reader:
                def __enter__(self):
                    self.stream = owner.path.open("rb")
                    return self

                def __exit__(self, *_):
                    self.stream.close()

                def read(self, size):
                    remaining = owner.limit - owner.raw.bytes_read
                    if not remaining:
                        raise indexer.ByteBudgetReached()
                    block = self.stream.read(min(size, remaining))
                    owner.raw.bytes_read += len(block)
                    owner.raw.sha256.update(block)
                    return block.decode(encoding)

            return Reader()

    api = SimpleNamespace(
        index=SimpleNamespace(
            BudgetedAnnotationPath=BudgetPath,
            ByteBudgetReached=indexer.ByteBudgetReached,
            canonical=indexer.canonical,
        ),
        f=fetch,
    )
    plan = {
        "routes": [
            {
                "annotation_index": 0,
                "image_path": selected["image_path"],
                "annotation_record_sha256": hashlib.sha256(indexer.canonical(selected)).hexdigest(),
            }
        ],
        "annotation_declared_not_rehashed": {"path": str(path)},
        "annotation_stat": audit.annotation_stat(path),
    }
    result, visited = audit.select_annotations(plan, api)
    assert result == {0: selected} and visited == 1
    selected["gpt_instruction"] = "altered record"
    path.write_bytes(audit.canonical([selected]) + b" " * 70000 + b"UNREAD_SUFFIX")
    plan["annotation_stat"] = audit.annotation_stat(path)
    with pytest.raises(ValueError):
        audit.select_annotations(plan, api)


def test_prefix_recheck_bounded_and_before_after_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    prefix = b"unchanged original prefix" * 10000
    source = tmp_path / "raw.json"
    source.write_bytes(prefix + b"unread suffix")
    monkeypatch.setattr(audit, "PREFIX_BYTES", len(prefix))
    monkeypatch.setattr(audit, "PREFIX_SHA", hashlib.sha256(prefix).hexdigest())
    plan = {
        "annotation_declared_not_rehashed": {"path": str(source)},
        "annotation_stat": audit.annotation_stat(source),
    }
    observed = audit.verify_prefix(plan)
    assert observed["bytes"] == len(prefix)
    assert observed["sha256"] == hashlib.sha256(prefix).hexdigest()
    source.write_bytes(b"X" + prefix[1:] + b"unread suffix")
    plan["annotation_stat"] = audit.annotation_stat(source)
    with pytest.raises(ValueError, match="prefix changed"):
        audit.verify_prefix(plan)


@pytest.mark.parametrize("mutation", ["missing_column", "row_count", "null_frame_index"])
def test_real_small_parquet_schema_and_metadata_gate(tmp_path, mutation):
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    rows = metadata("env_airsim_26/astar_data/low_short/tiny", actions(2))
    rows = [{**row, "image": {"bytes": png(), "path": None}} for row in rows]
    if mutation == "missing_column":
        table = pa.Table.from_pylist([{k: v for k, v in row.items() if k != "yaw"} for row in rows])
    else:
        if mutation == "null_frame_index":
            rows[0]["frame_index"] = None
        table = pa.Table.from_pylist(rows, schema=acquisition.expected_schema())
    path = tmp_path / "fixture.parquet"
    pq.write_table(table, path)
    if mutation != "null_frame_index":
        with pytest.raises(ValueError):
            acquisition.checked_parquet(path, 3 if mutation == "row_count" else 2)
    else:
        reader, _schema = acquisition.checked_parquet(path, 2)
        try:
            actual_rows = reader.read(columns=list(acquisition.METADATA_COLUMNS)).to_pylist()
        finally:
            reader.close()
        with pytest.raises(ValueError):
            geometry.metric_targets(actions(2), actual_rows, builder.derive_primitive)


@pytest.mark.parametrize(
    "raw_type, delta, target",
    [
        (
            "go up",
            [0.0, 0.0, 3.0],
            {"kind": "translate", "translation_m": [0.0, 0.0, -3.0], "yaw_delta_rad": None},
        ),
        (
            "go down",
            [0.0, 0.0, -3.0],
            {"kind": "translate", "translation_m": [0.0, 0.0, 3.0], "yaw_delta_rad": None},
        ),
    ],
)
def test_vertical_real_derive_seam_not_old_four_class(raw_type, delta, target):
    raw = actions(2)
    raw[0]["type"] = raw_type
    raw[1]["pos"] = delta
    actual, _ = builder.derive_primitive(raw, 0)
    assert actual == target


def test_import_entrypoint_is_stdlib_only():
    import subprocess
    import sys

    script = (
        "import runpy,sys; runpy.run_path(sys.argv[1],run_name='guarded'); "
        "assert not any(n.split('.')[0] in "
        "{'torch','transformers','peft','PIL','pyarrow','datasets'} for n in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", script, str(Path(audit.__file__))],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("args", [[], ["--plan", "missing"], ["--emit-plan", "x", "--plan", "y"]])
def test_cli_requires_one_explicit_mode(args):
    with pytest.raises(SystemExit):
        audit.main(args)
