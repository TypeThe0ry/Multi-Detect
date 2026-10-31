from __future__ import annotations

import copy
import io
import math
from types import SimpleNamespace

import pytest

pytest.importorskip("pyarrow", reason="OpenFly parquet acquisition tests require optional pyarrow")
pytest.importorskip(
    "huggingface_hub", reason="OpenFly parquet source binding requires optional huggingface_hub"
)
import pyarrow as pa
import pyarrow.parquet as pq
from test_bind_openfly_parquet_route_metadata import exact_source, shard_row  # noqa: F401
from test_fetch_openfly_multiscene_navigation import fixture_source, write  # noqa: F401

from scripts import bind_openfly_parquet_route_metadata as exact
from scripts import fetch_and_audit_openfly_parquet as parquet
from scripts import fetch_openfly_multiscene_navigation as acquisition
from scripts import plan_openfly_multiscene_acquisition as planner


def parquet_bytes(rows, schema=None):
    buffer = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(rows, schema=schema or parquet.expected_schema()), buffer)
    return buffer.getvalue()


class Response:
    def __init__(self, raw):
        self.raw = raw
        self.status_code = 200
        self.headers = {"Content-Length": str(len(raw))}
        self.request = SimpleNamespace(headers={})
        self.closed = False

    def iter_content(self, _size):
        yield self.raw

    def close(self):
        self.closed = True


class Client:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []
        self.responses = []

    def stream(self, url):
        self.calls.append(url)
        response = Response(self.payloads[url])
        self.responses.append(response)
        return response, []


@pytest.fixture
def parquet_source(exact_source, monkeypatch):  # noqa: F811
    value = exact_source
    monkeypatch.setattr(parquet, "ROOT", value.root)
    for name in parquet.SOURCE_FILES:
        write(value.root / name, b"new bounded CPU acquisition implementation\n")
    *_other, fetch, _builder = exact.helpers()
    payloads = copy.deepcopy(value.source.payloads)
    metadata_rows = {}
    for offset, route in enumerate(value.plan["routes"]):
        actions, _aim = fetch.parse_pose(
            payloads[fetch.dataset_url(route["pose_publisher"]["repository_path"])]
        )
        image_names = {
            int(row["path"].rsplit("_", 1)[1][:-4]): row["path"].rsplit("/", 1)[1][:-4]
            for row in value.source.trees["Image/" + route["image_path"]]
            if row["path"].endswith(".png")
        }
        rows = [
            {
                "env_id": route["image_path"].rsplit("/", 1)[0],
                "traj_id": route["image_path"].rsplit("/", 1)[1],
                "image_id": image_names[index],
                "frame_index": index,
                # These are deliberately NOT verified PNGs in this metadata-only phase.
                "image": {"bytes": b"not decoded or extracted", "path": None},
                "pos": [float(x) for x in action["pos"]],
                "yaw": float(action["yaw"]),
                "action_type": action["type"],
                "action_value": int(action["value"]),
            }
            for index, action in enumerate(actions)
        ]
        raw = parquet_bytes(rows)
        value.rows[offset] = shard_row(route["parquet_path"], raw)
        payloads[
            f"https://huggingface.co/datasets/{fetch.DATASET}/resolve/{fetch.REVISION}/{route['parquet_path']}"
        ] = raw
        metadata_rows[route["annotation_index"]] = rows
    exact.run_binding(value.path, value.sha, requester=value.requester)
    plan = parquet.build_plan()
    path = value.root / "configs/training/parquet-acquisition-plan.json"
    record = write(path, plan)
    return SimpleNamespace(
        root=value.root,
        source=value,
        plan=plan,
        path=path,
        sha=record["sha256"],
        client=Client(payloads),
        rows=metadata_rows,
    )


def audit_fixture(value, *, rows=None, schema=None, mutate_pose=None, mutate_config=None):
    route = value.plan["routes"][0]
    records = []
    for payload in value.plan["payloads"][:3]:
        raw = value.client.payloads[payload["url"]]
        if payload["role"] == "shard":
            raw = parquet_bytes(rows or value.rows[route["annotation_index"]], schema=schema)
        elif payload["role"] == "pose" and mutate_pose is not None:
            raw = mutate_pose(raw)
        elif payload["role"] == "config" and mutate_config is not None:
            raw = mutate_config(raw)
        file = write(value.root / "unit-audit" / payload["repository_path"], raw)
        records.append({**payload, "file": file})
    annotation = value.source.source.rows[route["annotation_index"]]
    return route, records, annotation


def test_original_train_paths_fixed_total_source_and_no_implicit_image_claim(parquet_source):
    value = parquet_source
    assert len(value.plan["payloads"]) == 9
    assert value.plan["expected_total_download_bytes"] == sum(
        row["bytes"] for row in value.plan["payloads"]
    )
    assert value.plan["expected_total_download_bytes"] < 128 * 1024 * 1024
    assert all(route["original_annotation_split"] == "train" for route in value.plan["routes"])
    assert not any(
        "seen.json" in row["url"] or "unseen.json" in row["url"] for row in value.plan["payloads"]
    )
    assert value.plan["embedded_image_column_materialized_or_extracted"] is False
    assert all(value is False for value in value.plan["claims"].values())


def test_actual_download_all_nine_sha_and_82_metadata_rows_cpu_only(parquet_source):
    value = parquet_source
    report, receipt = parquet.acquire(value.path, value.sha, client=value.client)
    assert report["native_exit_code"] == 0
    assert report["verified_payload_count"] == 9
    assert len(value.client.calls) == 9
    assert all(response.closed for response in value.client.responses)
    assert report["all_exact_original_payloads_verified"] is True
    assert report["all_metadata_rows_pose_aligned"] is True
    assert sum(len(audit["metadata_rows"]) for audit in report["metadata_audits"]) == 82
    assert report["sources_before"] == report["sources_after"]
    assert report["runtime_sources_before"] == report["runtime_sources_after"]
    assert report["full_shard_payloads_downloaded_and_byte_hashed"] is True
    assert report["embedded_image_column_materialized_or_extracted"] is False
    assert report["embedded_image_validity_verified"] is False
    assert report["forbidden_model_imports"] == []
    assert report["optimizer_updates"] == 0 and report["GPU_used"] is False
    assert all(report[key] is False for key in acquisition.GATES)
    assert not list((value.root / parquet.OUTPUT).rglob("*.png"))
    assert receipt == planner.binding(value.root / parquet.OUTPUT / "receipt.json")
    with pytest.raises(FileExistsError):
        parquet.acquire(value.path, value.sha, client=value.client)


@pytest.mark.parametrize(
    "change",
    [
        lambda plan: plan["payloads"].append(copy.deepcopy(plan["payloads"][0])),
        lambda plan: plan["payloads"][0].update(
            url="https://huggingface.co/datasets/IPEC-COMMUNITY/OpenFly/resolve/main/Annotation/unseen.json"
        ),
        lambda plan: plan["budget"].update(max_total_download_bytes=256 * 1024 * 1024),
        lambda plan: plan.update(output_directory="artifacts/training/old-source"),
        lambda plan: plan["claims"].update(training_ready=True),
    ],
)
def test_scope_mutations_rejected_before_download(parquet_source, change):
    value = parquet_source
    plan = copy.deepcopy(value.plan)
    change(plan)
    record = write(value.path, plan)
    with pytest.raises(ValueError, match="authority"):
        parquet.acquire(value.path, record["sha256"], client=value.client)
    assert value.client.calls == []
    assert not (value.root / parquet.OUTPUT).exists()


def test_original_publisher_digest_failure_retains_partial(parquet_source):
    value = parquet_source
    first = value.plan["payloads"][0]
    original = value.client.payloads[first["url"]]
    value.client.payloads[first["url"]] = original[:-1] + bytes([original[-1] ^ 1])
    report, _ = parquet.acquire(value.path, value.sha, client=value.client)
    assert report["native_exit_code"] == 1
    assert report["verified_payload_count"] == 0
    assert len(report["partial_files"]) == 1
    assert report["all_exact_original_payloads_verified"] is False
    assert report["metadata_audits"] == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("env_id", "env_airsim_18/astar_data/low_short"),
        ("traj_id", "different"),
        ("image_id", "20250101_175035_1"),
        ("frame_index", 1),
        ("pos", [99.0, 0.0, 0.0]),
        ("yaw", 0.4),
        ("action_type", "go left"),
        ("action_value", 5),
        ("yaw", None),
        ("pos", [0.0, None, 0.0]),
        ("yaw", math.nan),
    ],
)
def test_any_metadata_pose_identity_null_or_nonfinite_drift_fails(parquet_source, field, value):
    source = parquet_source
    route = source.plan["routes"][0]
    rows = copy.deepcopy(source.rows[route["annotation_index"]])
    rows[0][field] = value
    arguments = audit_fixture(source, rows=rows)
    with pytest.raises(ValueError):
        parquet.audit_metadata(*arguments)


def test_duplicate_missing_extra_frames_and_schema_type_fail(parquet_source):
    source = parquet_source
    route = source.plan["routes"][0]
    rows = copy.deepcopy(source.rows[route["annotation_index"]])
    rows[1] = copy.deepcopy(rows[0])
    with pytest.raises(ValueError, match="frame 1"):
        parquet.audit_metadata(*audit_fixture(source, rows=rows))
    rows = copy.deepcopy(source.rows[route["annotation_index"]])
    with pytest.raises(ValueError, match="row/row-group"):
        parquet.audit_metadata(*audit_fixture(source, rows=rows[:-1]))
    schema = parquet.expected_schema()
    schema = schema.set(schema.get_field_index("frame_index"), pa.field("frame_index", pa.int64()))
    with pytest.raises(ValueError, match="schema"):
        parquet.audit_metadata(*audit_fixture(source, schema=schema))


def test_fractional_original_action_value_is_not_truncated_to_int32(parquet_source):
    source = parquet_source

    def changed_pose(raw):
        return raw.replace(b'"value":3', b'"value":3.7', 1)

    with pytest.raises(ValueError, match="frame 0"):
        parquet.audit_metadata(*audit_fixture(source, mutate_pose=changed_pose))


def test_nonunit_scene_scale_never_guesses_units(parquet_source):
    with pytest.raises(ValueError, match="no units will be guessed"):
        parquet.audit_metadata(
            *audit_fixture(
                parquet_source,
                mutate_config=lambda raw: raw.replace(
                    b"traj_scale_ratio: 1", b"traj_scale_ratio: 2"
                ),
            )
        )


@pytest.mark.parametrize("kind", ["source_only", "output_only", "both"])
def test_payload_source_or_output_drift_is_native_failure(parquet_source, monkeypatch, kind):
    value = parquet_source
    original = parquet.audit_metadata
    modified = False

    def changed_audit(*args):
        nonlocal modified
        result = original(*args)
        if not modified:
            modified = True
            if kind != "output_only":
                (value.root / parquet.SOURCE_FILES[0]).write_bytes(b"changed during CPU data phase")
            if kind != "source_only":
                planner.local_path(args[1][0]["file"]["path"]).write_bytes(b"tampered output")
        return result

    monkeypatch.setattr(parquet, "audit_metadata", changed_audit)
    report, _ = parquet.acquire(value.path, value.sha, client=value.client)
    assert report["native_exit_code"] == 1
    assert report["sources_unchanged"] is (kind == "output_only")
    assert report["published_outputs_unchanged"] is (kind == "source_only")
    assert report["all_exact_original_payloads_verified"] is (kind == "source_only")
    assert report["all_metadata_rows_pose_aligned"] is False
    assert any(row["stage"] == "published_output_recheck" for row in report["failures"]) is (
        kind != "source_only"
    )


def test_cpu_schema_failure_does_not_erase_verified_downloads(parquet_source, monkeypatch):
    value = parquet_source

    def rejected_audit(*_args):
        raise ValueError("actual publisher schema is not admitted")

    monkeypatch.setattr(parquet, "audit_metadata", rejected_audit)
    report, _ = parquet.acquire(value.path, value.sha, client=value.client)
    assert report["native_exit_code"] == 1
    assert report["verified_payload_count"] == 9
    assert report["all_exact_original_payloads_verified"] is True
    assert report["published_outputs_unchanged"] is True
    assert len(report["failures"]) == 3
    assert report["all_metadata_rows_pose_aligned"] is False


def test_exact_download_allowlist_and_lfs_sha_failure(parquet_source):
    value = parquet_source
    row = next(row for row in value.plan["payloads"] if row["role"] == "shard")
    out = value.root / "unit-download"
    allowlist = {row["url"]: row}
    mutated = {**row, "url": row["url"].replace("traj/", "Annotation/unseen.json/")}
    with pytest.raises(ValueError, match="allowlist"):
        parquet.download_payload(value.client, mutated, out, allowlist)
    assert value.client.calls == []
    raw = value.client.payloads[row["url"]]
    value.client.payloads[row["url"]] = raw[:-1] + bytes([raw[-1] ^ 1])
    with pytest.raises(ValueError, match="digest"):
        parquet.download_payload(value.client, row, out, allowlist)
    assert len(list(out.rglob("*.partial"))) == 1
