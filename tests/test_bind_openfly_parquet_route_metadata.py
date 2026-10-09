from __future__ import annotations

import copy
import hashlib
from types import SimpleNamespace

import pytest
from test_fetch_openfly_multiscene_navigation import fixture_source, write  # noqa: F401

from scripts import bind_openfly_parquet_route_metadata as exact
from scripts import fetch_openfly_multiscene_navigation as acquisition
from scripts import plan_openfly_multiscene_acquisition as planner


def shard_row(path, raw=b"bounded parquet fixture"):
    return {
        "type": "file",
        "path": path,
        "size": len(raw),
        "oid": "a" * 40,
        "lfs": {"size": len(raw), "oid": hashlib.sha256(raw).hexdigest()},
    }


@pytest.fixture
def exact_source(fixture_source, monkeypatch):  # noqa: F811
    value = fixture_source
    monkeypatch.setattr(exact, "ROOT", value.root)
    leaf, _publication, probe, _acquisition, _planner, fetch, _builder = exact.helpers()
    for name in exact.SOURCE_FILES:
        write(value.root / name, b"new metadata-only fixture source\n")
    api_package = value.root / "fixture-package/huggingface_hub"
    write(api_package / "hf_api.py", b"fixture official API binding\n")
    monkeypatch.setattr(
        exact.importlib.util,
        "find_spec",
        lambda name: (
            SimpleNamespace(origin=str(api_package / "__init__.py"))
            if name == "huggingface_hub"
            else None
        ),
    )
    initial = acquisition.build_plan()
    initial_record = write(value.root / probe.INITIAL_PLAN, initial)
    original_routes = []
    for route in initial["routes"]:
        path = "Image/" + route["image_path"] + "/pose.jsonl"
        row = next(
            row for row in value.trees["Image/" + route["image_path"]] if row["path"] == path
        )
        original_routes.append(
            {
                "annotation_index": route["annotation_index"],
                "publisher_files": [
                    {"repository_path": path, **acquisition.metadata_record(row, cap=65536)}
                ],
            }
        )
    original_record = write(
        value.root / probe.ORIGINAL_OUTPUT / "report.json", {"routes": original_routes}
    )
    before = [initial_record, original_record]
    write(
        value.root / exact.LEAF_REPORT,
        {
            "contract": leaf.REPORT_CONTRACT,
            "dataset_revision": fetch.REVISION,
            "native_exit_code": 0,
            "sources_unchanged": True,
            "sources_before": before,
            "sources_after": copy.deepcopy(before),
            "parquet_payload_downloaded": False,
            "retained_metadata": [],
        },
    )
    plan = exact.build_plan()
    path = value.root / "configs/training/exact-route-plan.json"
    record = write(path, plan)
    rows = [shard_row(route["parquet_path"]) for route in plan["routes"]]
    calls = []

    def request(p):
        calls.append(copy.deepcopy(p))
        return copy.deepcopy(rows), planner.canonical(rows), {"content-type": "application/json"}

    return SimpleNamespace(
        source=value,
        root=value.root,
        plan=plan,
        path=path,
        sha=record["sha256"],
        rows=rows,
        calls=calls,
        requester=request,
    )


def test_plan_proves_original_train_annotation_episode_identity(exact_source):
    value = exact_source
    plan = value.plan
    assert [(row["annotation_index"], row["required_frame_count"]) for row in plan["routes"]] == [
        (139, 23),
        (220, 17),
        (90, 42),
    ]
    for route in plan["routes"]:
        annotation = value.source.rows[route["annotation_index"]]
        assert route["original_annotation_split"] == "train"
        assert route["parquet_path"] == "traj/" + annotation["image_path"] + ".parquet"
        assert (
            route["original_annotation_record_sha256"]
            == hashlib.sha256(planner.canonical(annotation)).hexdigest()
        )
        assert route["annotation_filenames"] == annotation["index_list"]
    assert plan["original_train_annotation"] == planner.binding(
        value.root / value.source.manifest["annotation"]["path"]
    )
    assert plan["budget"]["max_api_requests"] == 1
    assert plan["payload_download_authorized"] is False
    assert all(item is False for item in plan["claims"].values())
    assert value.calls == []


def test_only_exact_three_metadata_rows_success_and_create_only(exact_source):
    value = exact_source
    report, record = exact.run_binding(value.path, value.sha, requester=value.requester)
    assert report["native_exit_code"] == 0
    assert len(value.calls) == 1
    assert report["all_three_train_episode_paths_exist"] is True
    assert report["original_train_annotation_membership_bound"] is True
    assert report["sources_before"] == report["sources_after"]
    assert report["shard_bytes_sum"] == sum(row["size"] for row in value.rows)
    assert report["shard_contents_or_schema_verified"] is False
    assert report["reserved_seen_unseen_annotation_read"] is False
    assert report["payload_downloaded"] is False
    assert all(report[key] is False for key in acquisition.GATES)
    assert not list((value.root / exact.OUTPUT).rglob("*.parquet"))
    assert record == planner.binding(value.root / exact.OUTPUT / "report.json")
    with pytest.raises(FileExistsError):
        exact.run_binding(value.path, value.sha, requester=value.requester)
    assert len(value.calls) == 1


@pytest.mark.parametrize(
    "change",
    [
        lambda rows: rows.pop(),
        lambda rows: rows.append(copy.deepcopy(rows[0])),
        lambda rows: rows[1].update(path=rows[0]["path"]),
        lambda rows: rows[0].update(path="traj/env_airsim_18/other.parquet"),
        lambda rows: rows[0].pop("lfs"),
        lambda rows: rows[0]["lfs"].update(size=True),
        lambda rows: rows[0]["lfs"].update(oid="0" * 63),
        lambda rows: rows[0].update(size=True),
    ],
)
def test_missing_duplicate_unrequested_or_invalid_publisher_rows_fail(exact_source, change):
    value = exact_source
    change(value.rows)
    report, _ = exact.run_binding(value.path, value.sha, requester=value.requester)
    assert report["native_exit_code"] == 1
    assert report["all_three_train_episode_paths_exist"] is False
    assert report["original_train_annotation_membership_bound"] is False
    assert planner.verify_binding(report["metadata"]).is_file()
    assert report["payload_downloaded"] is False


def test_future_payload_sum_cap_is_not_permission_to_download(exact_source):
    value = exact_source
    for row in value.rows:
        row["size"] = row["lfs"]["size"] = 45 * 1024 * 1024
    report, _ = exact.run_binding(value.path, value.sha, requester=value.requester)
    assert report["native_exit_code"] == 1
    assert "128 MiB" in report["failure"]["reason"]
    assert report["original_train_annotation_membership_bound"] is False
    assert report["payload_downloaded"] is False


@pytest.mark.parametrize(
    "change",
    [
        lambda plan: plan["routes"][0].update(original_annotation_split="unseen"),
        lambda plan: plan["routes"][0].update(parquet_path="traj/unrequested.parquet"),
        lambda plan: plan.update(payload_download_authorized=True),
        lambda plan: plan.update(output_directory="artifacts/training/old-source"),
        lambda plan: plan["budget"].update(max_api_requests=2),
    ],
)
def test_explicit_scope_tampering_rejected_before_network(exact_source, change):
    value = exact_source
    plan = copy.deepcopy(value.plan)
    change(plan)
    record = write(value.path, plan)
    with pytest.raises(ValueError, match="authority"):
        exact.run_binding(value.path, record["sha256"], requester=value.requester)
    assert value.calls == []
    assert not (value.root / exact.OUTPUT).exists()


def test_plan_hash_and_input_drift_rejected_before_network(exact_source):
    value = exact_source
    with pytest.raises(ValueError, match="SHA"):
        exact.run_binding(value.path, "0" * 64, requester=value.requester)
    (value.root / exact.SOURCE_FILES[0]).write_bytes(b"changed")
    with pytest.raises(ValueError):
        exact.run_binding(value.path, value.sha, requester=value.requester)
    assert value.calls == []


def test_source_drift_during_request_retains_failure_report(exact_source):
    value = exact_source

    def changed_request(plan):
        result = value.requester(plan)
        (value.root / exact.SOURCE_FILES[0]).write_bytes(b"changed after admission")
        return result

    report, _ = exact.run_binding(value.path, value.sha, requester=changed_request)
    assert report["native_exit_code"] == 1
    assert report["sources_unchanged"] is False
    assert report["original_train_annotation_membership_bound"] is False


def test_document_cannot_differ_from_retained_metadata(exact_source):
    value = exact_source

    def mismatched_request(_plan):
        return value.rows, b"[]", {}

    report, _ = exact.run_binding(value.path, value.sha, requester=mismatched_request)
    assert report["native_exit_code"] == 1
    assert report["routes"] == []
    assert planner.verify_binding(report["metadata"]).read_bytes() == b"[]"


class PublicResponse:
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


class PublicSession:
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.auth = None
        self.closed = False

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.response

    def close(self):
        self.closed = True


@pytest.mark.parametrize(
    "failure", [None, "status", "auth", "length", "encoding", "stream_cap", "truncated"]
)
def test_public_request_auth_redirect_and_stream_caps(exact_source, monkeypatch, failure):
    value = exact_source
    response = PublicResponse(planner.canonical(value.rows))
    if failure == "status":
        response.status_code = 302
    elif failure == "auth":
        response.request.headers["Authorization"] = "fixture secret"
    elif failure == "length":
        response.headers["Content-Length"] = str(exact.BUDGET["max_metadata_bytes"] + 1)
    elif failure == "encoding":
        response.headers["Content-Encoding"] = "gzip"
    elif failure == "stream_cap":
        response.headers.pop("Content-Length")
        response.raw = b"x" * (exact.BUDGET["max_metadata_bytes"] + 1)
    elif failure == "truncated":
        response.headers["Content-Length"] = str(len(response.raw) + 1)
    session = PublicSession(response)
    *_others, fetch, _builder = exact.helpers()
    monkeypatch.setattr(fetch, "OfficialClient", lambda: SimpleNamespace(session=session))
    if failure is None:
        document, raw, _headers = exact.public_request(value.plan)
        assert document == value.rows
        assert raw == planner.canonical(value.rows)
    else:
        with pytest.raises(ValueError):
            exact.public_request(value.plan)
    args, kwargs = session.calls[0]
    assert args == (value.plan["metadata_endpoint"],)
    assert kwargs["allow_redirects"] is False
    assert kwargs["stream"] is True
    assert kwargs["data"]["paths"] == [row["parquet_path"] for row in value.plan["routes"]]
    assert kwargs["headers"]["Accept-Encoding"] == "identity"
    assert callable(session.auth)
    assert response.closed is True and session.closed is True
