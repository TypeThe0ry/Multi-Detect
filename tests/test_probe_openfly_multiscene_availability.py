from __future__ import annotations

import copy
import sys
from types import SimpleNamespace

import pytest
from test_fetch_openfly_multiscene_navigation import (
    Client,
    Response,
    fixture_source,
    publisher_entry,
    write,
)

from scripts import fetch_openfly_multiscene_navigation as acquisition
from scripts import plan_openfly_multiscene_acquisition as planner
from scripts import probe_openfly_multiscene_availability as probe


@pytest.fixture
def local_probe(tmp_path, monkeypatch):
    source = fixture_source.__wrapped__(tmp_path, monkeypatch)
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    for name in probe.SOURCE_FILES:
        write(tmp_path / name, b"fixture probe implementation\n")
    readme = b"# OpenFly\nThe dataset has been converted to parquet format.\n"
    code_tree_path = source.source / "metadata/code-tree.json"
    code_tree = planner.strict_json(code_tree_path.read_bytes())
    code_tree["tree"].append(publisher_entry("README.md", readme, kind="blob"))
    code_tree_binding = write(code_tree_path, code_tree)
    extra_indices = {scene: [] for _index, scene, _partition, _frames in acquisition.ROUTES}
    cursor = 0
    for original_index, scene, _partition, _frames in acquisition.ROUTES:
        for ordinal in range(7):
            row = copy.deepcopy(source.rows[original_index])
            row["image_path"] = row["image_path"].rsplit("/", 1)[0] + f"/extra-{ordinal}"
            source.rows[cursor] = row
            extra_indices[scene].append(cursor)
            source.trees["Image/" + row["image_path"]] = [
                publisher_entry("Image/" + row["image_path"] + "/pose.jsonl", b"pose fixture")
            ]
            cursor += 1
    annotation = write(source.source / "raw/Annotation/train.json", source.rows)
    fetch, _builder = planner.helpers()
    monkeypatch.setattr(fetch, "ANNOTATION_BYTES", annotation["bytes"])
    monkeypatch.setattr(fetch, "ANNOTATION_SHA256", annotation["sha256"])
    source.manifest["annotation"] = annotation
    source.manifest["metadata_records"] = [code_tree_binding]
    manifest_binding = write(source.manifest_path, source.manifest)
    source.preflight["source_manifest"] = manifest_binding
    preflight_binding = write(tmp_path / acquisition.PREFLIGHT_PLAN, source.preflight)
    source.report["plan"] = preflight_binding
    source.report["sources_before"] = sorted(
        planner.source_records(source.manifest) + [manifest_binding, preflight_binding],
        key=lambda row: row["path"],
    )
    source.report["sources_after"] = copy.deepcopy(source.report["sources_before"])
    write(tmp_path / acquisition.PREFLIGHT_REPORT, source.report)
    download_plan = acquisition.build_plan()
    download_binding = write(tmp_path / probe.INITIAL_PLAN, download_plan)
    first = "Image/" + download_plan["routes"][0]["image_path"]
    attempt_client = Client(copy.deepcopy(source.trees), source.payloads)
    attempt_client.trees[first] = attempt_client.trees[first][:1]
    receipt, _receipt_binding = acquisition.acquire(
        tmp_path / probe.INITIAL_PLAN, download_binding["sha256"], client=attempt_client
    )
    assert receipt["native_exit_code"] == 1 and len(attempt_client.tree_calls) == 1
    client = Client(source.trees, source.payloads)
    readme_url = (
        f"https://raw.githubusercontent.com/{fetch.CODE_REPOSITORY}/{fetch.CODE_REVISION}/README.md"
    )
    client.payloads[readme_url] = readme
    return SimpleNamespace(source=source, client=client, extra_indices=extra_indices, readme=readme)


def save_plan(value, mode):
    plan = probe.build_plan(mode)
    path = value.source.root / f"configs/training/metadata-probe-{mode}.json"
    record = write(path, plan)
    return path, record["sha256"], plan


def run_original(value):
    path, digest, _plan = save_plan(value, "original")
    return probe.run_probe(path, digest, client=value.client)


def test_original_diagnostic_does_not_hide_other_two_routes(local_probe):
    value = local_probe
    report, record = run_original(value)
    assert report["native_exit_code"] == 0
    assert report["tree_requests_performed"] == 2
    assert [row["annotation_index"] for row in report["routes"]] == [139, 220, 90]
    assert report["pose_only_routes"] == [139]
    assert report["complete_metadata_routes"] == [220, 90]
    assert report["sources_before"] == report["sources_after"]
    assert report["payload_download_performed"] is False
    assert all(report[key] is False for key in acquisition.GATES)
    assert value.client.stream_calls == []
    assert record == planner.binding(value.source.root / probe.ORIGINAL_OUTPUT / "report.json")
    assert report["routes"][0]["acquisition_method"] == "retained_bound_tree_no_request"


def test_expansion_is_explicit_24_candidates_and_21_requests(local_probe):
    value = local_probe
    run_original(value)
    value.client.tree_calls.clear()
    path, digest, plan = save_plan(value, "availability")
    assert len(plan["candidates"]) == 24
    for scene in value.extra_indices:
        chosen = [row for row in plan["candidates"] if row["scene_id"] == scene]
        assert len(chosen) == 8
        assert [row["annotation_index"] for row in chosen[1:]] == value.extra_indices[scene]
    report, _ = probe.run_probe(path, digest, client=value.client)
    assert report["native_exit_code"] == 0
    assert report["tree_requests_performed"] == 21
    assert len(report["pose_only_routes"]) == 22
    assert report["complete_metadata_routes"] == [220, 90]
    assert report["payload_download_performed"] is False
    assert report["independent_holdout_available"] is False
    assert report["sources_before"] == report["sources_after"]
    assert len(value.client.stream_calls) == 1
    assert value.client.stream_calls[0].endswith("/README.md")
    assert report["official_code_readme"]["bytes"] == len(value.readme)
    assert report["actual_retained_metadata_bytes"] < probe.BUDGET["max_total_metadata_bytes"]


def test_availability_requires_original_diagnostics(local_probe):
    with pytest.raises(FileNotFoundError):
        probe.build_plan("availability")
    assert not (local_probe.source.root / probe.AVAILABILITY_OUTPUT).exists()


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p["budget"].update(max_tree_requests=25),
        lambda p: p.update(output_directory=acquisition.OUTPUT),
        lambda p: p.update(payload_download_authorized=True),
        lambda p: p["candidates"][0].update(scene_id="env_airsim_18"),
        lambda p: p["candidates"].append(copy.deepcopy(p["candidates"][0])),
        lambda p: p["claims"].update(training_ready=True),
        lambda p: p["sources"].pop(),
    ],
)
def test_tampered_probe_fails_before_network(local_probe, change):
    value = local_probe
    path, _digest, plan = save_plan(value, "original")
    change(plan)
    record = write(path, plan)
    with pytest.raises(ValueError):
        probe.run_probe(path, record["sha256"], client=value.client)
    assert value.client.tree_calls == [] and value.client.stream_calls == []
    assert not (value.source.root / probe.ORIGINAL_OUTPUT).exists()


def test_unknown_original_route_does_not_hide_last_route(local_probe):
    value = local_probe
    original_tree = value.client.tree
    bad_name = "Image/" + value.source.rows[220]["image_path"]

    def failed_tree(path):
        if path == bad_name:
            raise RuntimeError("network fixture")
        return original_tree(path)

    value.client.tree = failed_tree
    report, _ = run_original(value)
    assert report["native_exit_code"] == 1
    assert report["unknown_routes"] == [220]
    assert report["routes"][2]["status"] == "complete_rgb_pose_tree_not_downloaded"
    assert report["payload_download_performed"] is False


def test_source_drift_during_metadata_is_native_failure(local_probe):
    value = local_probe
    value.client.after_tree = lambda _path: (value.source.root / probe.SOURCE_FILES[0]).write_bytes(
        b"drift"
    )
    report, _ = run_original(value)
    assert report["native_exit_code"] == 1
    assert report["sources_unchanged"] is False
    assert all(report[key] is False for key in acquisition.GATES)


def test_deleted_source_is_reported_not_fabricated(local_probe):
    value = local_probe
    source_path = value.source.root / probe.SOURCE_FILES[0]
    value.client.after_tree = lambda _path: source_path.unlink(missing_ok=True)
    report, _ = run_original(value)
    assert report["native_exit_code"] == 1
    assert report["sources_unchanged"] is False
    assert report["source_errors"][0]["error_type"] == "FileNotFoundError"
    assert len(report["sources_after"]) < len(report["sources_before"])


def test_invalid_retained_metadata_is_preserved_with_unknown_status(local_probe):
    value = local_probe
    candidate = "Image/" + value.source.rows[220]["image_path"]
    value.client.trees[candidate][0]["path"] = "Image/env_airsim_18/escape/pose.jsonl"
    report, _ = run_original(value)
    assert report["native_exit_code"] == 1
    failed = report["routes"][1]
    assert failed["status"] == "unknown" and failed["tree"] is not None
    assert planner.verify_binding(failed["tree"]).is_file()


def test_complete_metadata_does_not_assert_pose_or_units(local_probe):
    report, _ = run_original(local_probe)
    candidate = report["routes"][1]
    assert candidate["complete_rgb_pose_tree"] is True
    assert candidate["raw_pose_alignment_verified"] is False
    assert candidate["scene_units_verified"] is False
    assert candidate["publisher_payload_bytes"] > 0
    assert len(candidate["publisher_files"]) == 18


@pytest.mark.parametrize("length", [None, "bad", "2097153"])
def test_official_metadata_reader_rejects_unbounded_lengths(local_probe, monkeypatch, length):
    _acquisition, _planner, fetch, _builder = probe.helpers()
    response = Response(b"[]")
    if length is None:
        response.headers.clear()
    else:
        response.headers["Content-Length"] = length
    monkeypatch.setattr(fetch.OfficialClient, "stream", lambda _self, _url: (response, []))
    client = probe.bounded_client()
    try:
        with pytest.raises(ValueError, match="remaining budget"):
            client.metadata("https://huggingface.co/api/datasets/fixture")
    finally:
        client.session.close()
    assert response.closed


def test_official_metadata_shared_remaining_budget(local_probe, monkeypatch):
    _acquisition, _planner, fetch, _builder = probe.helpers()
    response = Response(b"[]")
    monkeypatch.setattr(fetch.OfficialClient, "stream", lambda _self, _url: (response, []))
    client = probe.bounded_client()
    client.remaining_metadata_bytes = 1
    try:
        with pytest.raises(ValueError):
            client.metadata("https://huggingface.co/api/datasets/fixture")
    finally:
        client.session.close()
    assert response.closed


def test_pinned_document_digest_failure_is_not_probe_success(local_probe):
    value = local_probe
    run_original(value)
    path, digest, _ = save_plan(value, "availability")
    url = next(url for url in value.client.payloads if url.endswith("/README.md"))
    value.client.payloads[url] = b"x" * len(value.readme)
    report, _ = probe.run_probe(path, digest, client=value.client)
    assert report["native_exit_code"] == 1
    assert report["metadata_probe_completed"] is False
    assert report["tree_requests_performed"] == 0
    assert "pinned publisher" in report["failure"]["reason"]


def test_probe_create_only_and_path_restoration(local_probe):
    before = sys.path.copy()
    probe.helpers()
    assert sys.path == before
    run_original(local_probe)
    path, digest, _ = save_plan(local_probe, "original")
    with pytest.raises(FileExistsError):
        probe.run_probe(path, digest, client=local_probe.client)
