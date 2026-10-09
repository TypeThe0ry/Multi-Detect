from __future__ import annotations

import copy
import hashlib
from types import SimpleNamespace

import pytest
from test_fetch_openfly_multiscene_navigation import Response, publisher_entry, write

from scripts import fetch_openfly_multiscene_navigation as acquisition
from scripts import plan_openfly_multiscene_acquisition as planner
from scripts import probe_openfly_parquet_publication as publication


class Client:
    def __init__(self, trees, documents):
        self.trees = trees
        self.documents = documents
        self.calls = []
        self.stream_calls = []
        self.callback = None

    def metadata(self, url):
        directory = url.split("/tree/", 1)[1].split("/", 1)[1].split("?", 1)[0]
        self.calls.append(directory)
        if self.callback is not None:
            self.callback(directory)
        document = self.trees[directory]
        return copy.deepcopy(document), planner.canonical(document), []

    def stream(self, url):
        self.stream_calls.append(url)
        return Response(self.documents[url]), []


@pytest.fixture
def fixture_publication(tmp_path, monkeypatch):
    monkeypatch.setattr(publication, "ROOT", tmp_path)
    monkeypatch.setattr(planner, "ROOT", tmp_path)
    monkeypatch.setattr(acquisition, "ROOT", tmp_path)
    _probe, _acquisition, _planner, fetch, _builder = publication.helpers()
    source = write(tmp_path / "metadata/input.json", {"source": "retained pinned fixture"})
    documents, declarations = {}, []
    for name in publication.DOCUMENT_NAMES:
        raw = b"# Publisher documentation; never execute.\n"
        url = f"https://huggingface.co/datasets/{fetch.DATASET}/resolve/{fetch.REVISION}/{name}"
        documents[url] = raw
        declarations.append(
            {
                "repository_path": name,
                "url": url,
                "bytes": len(raw),
                "digest": hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest(),
                "digest_kind": "sha1-git-blob",
            }
        )
    plan = {
        "contract": publication.CONTRACT,
        "dataset_repository": fetch.DATASET,
        "dataset_revision": fetch.REVISION,
        "output_directory": publication.OUTPUT,
        "budget": publication.BUDGET.copy(),
        "sources": [source],
        "documentation": declarations,
        "payload_download_authorized": False,
        "claims": acquisition.GATES.copy(),
    }
    monkeypatch.setattr(publication, "build_plan", lambda: copy.deepcopy(plan))
    trees = {
        "traj": [{"type": "directory", "path": "traj/env_airsim_26"}],
        "traj/env_airsim_26": [
            publisher_entry("traj/env_airsim_26/train-00000.parquet", b"never downloaded"),
            publisher_entry("traj/env_airsim_26/train-00001.parquet", b"never downloaded larger"),
        ],
    }
    path = tmp_path / "configs/training/publication-probe.json"
    record = write(path, plan)
    return SimpleNamespace(
        root=tmp_path,
        path=path,
        digest=record["sha256"],
        plan=plan,
        client=Client(trees, documents),
        source=source,
    )


def test_two_level_metadata_inventory_never_streams_parquet(fixture_publication):
    value = fixture_publication
    report, record = publication.run_probe(value.path, value.digest, client=value.client)
    assert report["native_exit_code"] == 0
    assert value.client.calls == ["traj", "traj/env_airsim_26"]
    assert report["observed_parquet_shard_count"] == 2
    assert report["observed_parquet_bytes"] == 39
    assert report["smallest_observed_shard_bytes"] == 16
    assert report["two_level_inventory_exhaustive"] is True
    assert report["sources_before"] == report["sources_after"]
    assert report["parquet_payload_downloaded"] is False
    assert report["rgb_parquet_schema_verified"] is False
    assert all(report[key] is False for key in acquisition.GATES)
    assert all(
        url.rsplit("/", 1)[1] in publication.DOCUMENT_NAMES for url in value.client.stream_calls
    )
    assert record == planner.binding(value.root / publication.OUTPUT / "report.json")
    assert not list((value.root / publication.OUTPUT).rglob("*.parquet"))


def test_six_request_cap_and_unvisited_children_not_exhaustive(fixture_publication):
    value = fixture_publication
    value.client.trees["traj"] = [
        {"type": "directory", "path": f"traj/scene-{i}"} for i in range(7)
    ]
    for i in range(7):
        value.client.trees[f"traj/scene-{i}"] = [
            publisher_entry(f"traj/scene-{i}/part.parquet", b"x")
        ]
    report, _ = publication.run_probe(value.path, value.digest, client=value.client)
    assert report["native_exit_code"] == 0
    assert report["tree_requests_performed"] == 6
    assert len(value.client.calls) == 6
    assert report["two_level_inventory_exhaustive"] is False
    assert report["observed_parquet_shard_count"] == 5


def test_uninspected_third_level_is_not_falsely_complete(fixture_publication):
    value = fixture_publication
    value.client.trees["traj/env_airsim_26"] = [
        {"type": "directory", "path": "traj/env_airsim_26/deeper"}
    ]
    report, _ = publication.run_probe(value.path, value.digest, client=value.client)
    assert report["native_exit_code"] == 0
    assert report["observed_parquet_shard_count"] == 0
    assert report["smallest_observed_shard_bytes"] is None
    assert report["two_level_inventory_exhaustive"] is False
    assert len(value.client.calls) == 2


def test_pagination_boundary_remains_observed_not_total(fixture_publication):
    value = fixture_publication
    value.client.trees["traj/env_airsim_26"] = [
        publisher_entry(f"traj/env_airsim_26/part-{i}.parquet", b"x") for i in range(1000)
    ]
    report, _ = publication.run_probe(value.path, value.digest, client=value.client)
    assert report["native_exit_code"] == 0
    assert report["observed_parquet_shard_count"] == 1000
    assert report["two_level_inventory_exhaustive"] is False
    assert len(value.client.calls) == 2


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p["budget"].update(max_tree_requests=7),
        lambda p: p.update(payload_download_authorized=True),
        lambda p: p.update(output_directory="artifacts/training/old-source"),
        lambda p: p["claims"].update(training_ready=True),
        lambda p: p["documentation"][0].update(repository_path="weights.safetensors"),
        lambda p: p["sources"].pop(),
    ],
)
def test_plan_tamper_rejected_before_request(fixture_publication, mutation):
    value = fixture_publication
    changed = copy.deepcopy(value.plan)
    mutation(changed)
    record = write(value.path, changed)
    with pytest.raises(ValueError):
        publication.run_probe(value.path, record["sha256"], client=value.client)
    assert value.client.calls == [] and value.client.stream_calls == []
    assert not (value.root / publication.OUTPUT).exists()


def test_source_drift_at_request_does_not_produce_success(fixture_publication):
    value = fixture_publication
    value.client.callback = lambda _directory: (value.root / value.source["path"]).write_bytes(
        b"changed"
    )
    report, _ = publication.run_probe(value.path, value.digest, client=value.client)
    assert report["native_exit_code"] == 1
    assert report["sources_unchanged"] is False
    assert report["two_level_inventory_exhaustive"] is False


def test_invalid_tree_raw_retained_with_native_failure(fixture_publication):
    value = fixture_publication
    value.client.trees["traj/env_airsim_26"][0]["path"] = "outside/escape.parquet"
    report, _ = publication.run_probe(value.path, value.digest, client=value.client)
    assert report["native_exit_code"] == 1
    assert len(report["retained_metadata"]) == 2
    assert planner.verify_binding(report["retained_metadata"][-1]).is_file()
    assert report["parquet_payload_downloaded"] is False


def test_document_publisher_digest_failure_not_success(fixture_publication):
    value = fixture_publication
    url = next(iter(value.client.documents))
    value.client.documents[url] = b"x" * len(value.client.documents[url])
    report, _ = publication.run_probe(value.path, value.digest, client=value.client)
    assert report["native_exit_code"] == 1
    assert "digest" in report["errors"][0]["reason"]
    assert report["documentation"] == []


def test_large_parquet_metadata_is_not_payload_authorization(fixture_publication):
    value = fixture_publication
    entry = value.client.trees["traj/env_airsim_26"][0]
    entry["size"] = 5 * 1024**3
    entry["lfs"] = {"size": entry["size"], "oid": "1" * 64}
    report, _ = publication.run_probe(value.path, value.digest, client=value.client)
    assert report["native_exit_code"] == 0
    assert report["observed_parquet_shards"][0]["bytes"] == 5 * 1024**3
    assert report["observed_parquet_shards"][0]["digest"] == "1" * 64
    assert report["actual_metadata_bytes"] < 10000
    assert report["parquet_payload_downloaded"] is False


def test_output_create_only(fixture_publication):
    value = fixture_publication
    publication.run_probe(value.path, value.digest, client=value.client)
    with pytest.raises(FileExistsError):
        publication.run_probe(value.path, value.digest, client=value.client)


def test_api_reader_keeps_pagination_signal_without_logging_link(fixture_publication):
    response = Response(b"[]")
    response.headers["Link"] = "https://huggingface.co/api/next?cursor=private; rel=next"
    client = SimpleNamespace(stream=lambda _url: (response, []))
    tree, raw, trail, paginated = publication.tree_metadata(
        client, "https://huggingface.co/api/fixture", 100
    )
    assert tree == [] and raw == b"[]"
    assert paginated is True and response.closed
    assert trail == []
