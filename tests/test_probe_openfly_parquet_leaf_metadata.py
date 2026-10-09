from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from test_fetch_openfly_multiscene_navigation import publisher_entry, write
from test_probe_openfly_parquet_publication import Client

from scripts import fetch_openfly_multiscene_navigation as acquisition
from scripts import plan_openfly_multiscene_acquisition as planner
from scripts import probe_openfly_parquet_leaf_metadata as leaf


@pytest.fixture
def local_leaf(tmp_path, monkeypatch):
    monkeypatch.setattr(leaf, "ROOT", tmp_path)
    monkeypatch.setattr(planner, "ROOT", tmp_path)
    monkeypatch.setattr(acquisition, "ROOT", tmp_path)
    _publication, _probe, _acquisition, _planner, fetch, _builder = leaf.helpers()
    source = write(tmp_path / "metadata/input.json", {"source": "pinned fixture"})
    bases = [f"traj/{scene}/astar_data" for scene in leaf.SCENES]
    plan = {
        "contract": leaf.CONTRACT,
        "dataset_repository": fetch.DATASET,
        "dataset_revision": fetch.REVISION,
        "base_directories": bases,
        "output_directory": leaf.OUTPUT,
        "budget": leaf.BUDGET.copy(),
        "sources": [source],
        "payload_download_authorized": False,
        "claims": acquisition.GATES.copy(),
    }
    monkeypatch.setattr(leaf, "build_plan", lambda: copy.deepcopy(plan))
    trees = {}
    for base in bases:
        trees[base] = [
            {"type": "directory", "path": base + "/high_long"},
            {"type": "directory", "path": base + "/low_short"},
        ]
        trees[base + "/low_short"] = [
            publisher_entry(base + "/low_short/episode.parquet", b"shard")
        ]
    path = tmp_path / "configs/training/leaf-plan.json"
    record = write(path, plan)
    return SimpleNamespace(
        root=tmp_path,
        plan=plan,
        path=path,
        digest=record["sha256"],
        source=source,
        client=Client(trees, {}),
    )


def test_exact_six_metadata_requests_prioritize_real_low_short(local_leaf):
    value = local_leaf
    report, record = leaf.run_probe(value.path, value.digest, client=value.client)
    assert report["native_exit_code"] == 0
    assert report["tree_requests_performed"] == 6
    assert report["selected_category_trees_complete"] is True
    assert report["whole_scene_inventory_exhaustive"] is False
    assert report["whole_dataset_inventory_exhaustive"] is False
    assert report["observed_parquet_shard_count"] == 3
    assert report["smallest_observed_shard_bytes"] == 5
    assert report["observed_parquet_bytes"] == 15
    assert all(choice["selection_reason"] == "published_low_short" for choice in report["choices"])
    assert report["sources_before"] == report["sources_after"]
    assert all(report[key] is False for key in acquisition.GATES)
    assert value.client.stream_calls == []
    assert not list((value.root / leaf.OUTPUT).rglob("*.parquet"))
    assert record == planner.binding(value.root / leaf.OUTPUT / "report.json")


def test_missing_low_short_uses_actual_directory_not_invented(local_leaf):
    value = local_leaf
    base = value.plan["base_directories"][0]
    value.client.trees[base].pop()
    value.client.trees[base + "/high_long"] = [
        publisher_entry(base + "/high_long/actual.parquet", b"actual")
    ]
    report, _ = leaf.run_probe(value.path, value.digest, client=value.client)
    assert report["native_exit_code"] == 0
    assert report["choices"][0]["selected_category"] == base + "/high_long"
    assert report["choices"][0]["selection_reason"] == "first_actual_publisher_directory"
    assert base + "/low_short" not in value.client.calls


def test_no_category_does_not_invent_leaf_request(local_leaf):
    value = local_leaf
    value.client.trees[value.plan["base_directories"][0]] = []
    report, _ = leaf.run_probe(value.path, value.digest, client=value.client)
    assert report["native_exit_code"] == 0
    assert report["tree_requests_performed"] == 5
    assert report["choices"][0]["selected_category"] is None
    assert report["selected_category_trees_complete"] is False


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p["budget"].update(max_tree_requests=7),
        lambda p: p["base_directories"].append("traj/env_airsim_18/astar_data"),
        lambda p: p.update(output_directory="artifacts/training/old-source"),
        lambda p: p.update(payload_download_authorized=True),
        lambda p: p["claims"].update(training_ready=True),
    ],
)
def test_scope_tamper_rejected_before_requests(local_leaf, change):
    value = local_leaf
    plan = copy.deepcopy(value.plan)
    change(plan)
    record = write(value.path, plan)
    with pytest.raises(ValueError):
        leaf.run_probe(value.path, record["sha256"], client=value.client)
    assert value.client.calls == []


def test_partial_or_paginated_leaf_is_not_complete_inventory(local_leaf):
    value = local_leaf
    base = value.plan["base_directories"][0] + "/low_short"
    value.client.trees[base] = [publisher_entry(base + f"/{i}.parquet", b"x") for i in range(1000)]
    report, _ = leaf.run_probe(value.path, value.digest, client=value.client)
    assert report["native_exit_code"] == 0
    assert report["selected_category_trees_complete"] is False
    assert report["observed_parquet_shard_count"] == 1002


def test_source_drift_marks_real_native_failure(local_leaf):
    value = local_leaf
    value.client.callback = lambda _path: (value.root / value.source["path"]).write_bytes(b"drift")
    report, _ = leaf.run_probe(value.path, value.digest, client=value.client)
    assert report["native_exit_code"] == 1
    assert report["sources_unchanged"] is False
    assert report["selected_category_trees_complete"] is False


def test_escaping_tree_retained_without_payload_download(local_leaf):
    value = local_leaf
    base = value.plan["base_directories"][0] + "/low_short"
    value.client.trees[base][0]["path"] = "Annotation/unseen.json"
    report, _ = leaf.run_probe(value.path, value.digest, client=value.client)
    assert report["native_exit_code"] == 1
    assert len(report["retained_metadata"]) == 4
    assert planner.verify_binding(report["retained_metadata"][-1]).is_file()
    assert report["range_or_footer_requested"] is False
    assert report["parquet_payload_downloaded"] is False


def test_leaf_output_create_only(local_leaf):
    value = local_leaf
    leaf.run_probe(value.path, value.digest, client=value.client)
    with pytest.raises(FileExistsError):
        leaf.run_probe(value.path, value.digest, client=value.client)
