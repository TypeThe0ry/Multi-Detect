"""Small synthetic payloads/stand-ins only: no HTTP, Parquet/PNG or ML proof."""

from __future__ import annotations

import copy
import hashlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import fetch_and_audit_openfly_parquet as downloader
from scripts import fetch_openfly_indexed_training_routes as indexed
from scripts import fetch_openfly_multiscene_navigation as acquisition
from scripts import fetch_openfly_navigation_pilot as fetch
from scripts import plan_openfly_multiscene_acquisition as planner


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = indexed.canonical(value) + b"\n" if not isinstance(value, bytes) else value
    path.write_bytes(raw)
    return {"path": str(path), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


class Response:
    def __init__(self, raw, *, headers=None, status=200, authorization=False):
        self.raw = raw
        self.status_code = status
        self.headers = {"Content-Length": str(len(raw))} if headers is None else headers
        self.request = SimpleNamespace(headers={"Authorization": "secret"} if authorization else {})
        self.closed = False

    def iter_content(self, size):
        assert size == 1024 * 1024
        for offset in range(0, len(self.raw), 7):
            yield self.raw[offset : offset + 7]

    def close(self):
        self.closed = True


class Client:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []
        self.responses = []
        self.transform = lambda response: response
        self.after_stream = lambda: None
        self.session = SimpleNamespace(auth=None, close=lambda: None)

    def stream(self, url):
        self.calls.append(url)
        response = self.transform(Response(self.payloads[url]))
        self.responses.append(response)
        self.after_stream()
        return response, [
            {"host": "huggingface.co", "headers": {"Location": "https://signed.invalid/?secret"}},
            {"host": "cas-bridge.xethub.hf.co", "url": "https://signed.invalid/?secret"},
        ]


@pytest.fixture
def source(tmp_path, monkeypatch):
    # Scoped module globals are only a synthetic repository injection. Frozen
    # helper files are never edited; the actual download_payload function runs.
    for module in (indexed, downloader, acquisition, planner):
        monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(indexed, "helpers", lambda: (downloader, acquisition, planner, fetch))
    monkeypatch.setattr(
        downloader, "helpers", lambda: (None, None, None, None, acquisition, planner, fetch, None)
    )
    # A combined pytest process can have unrelated image/ML modules collected.
    # Worker import exclusion is separately tested; orchestration is a stand-in.
    monkeypatch.setattr(indexed, "no_heavy_imports", lambda: None)
    for name in (*indexed.SOURCE_FILES, *(f"scripts/{name}.py" for name in indexed.PINNED_HELPERS)):
        write(tmp_path / name, b"fixture source identity only\n")
    small = write(tmp_path / "index-source.json", b"fixture pre-existing small source\n")
    payloads, rows, published = {}, [], []
    for offset, record in enumerate(indexed.PUBLISHED):
        number, name, frames, annotation_sha, *_publisher = record
        image_path = f"env_airsim_26/astar_data/low_short/{name}"
        shard = f"fixture not-a-parquet {offset}".encode()
        pose = f"fixture not-a-pose {offset}".encode()
        shard_sha = hashlib.sha256(shard).hexdigest()
        pose_sha = hashlib.sha1(f"blob {len(pose)}\0".encode() + pose).hexdigest()
        published.append(
            (number, name, frames, annotation_sha, len(shard), shard_sha, len(pose), pose_sha)
        )
        for path, raw in (
            (f"traj/{image_path}.parquet", shard),
            (f"Image/{image_path}/pose.jsonl", pose),
        ):
            payloads[
                f"https://huggingface.co/datasets/{indexed.DATASET}/resolve/{indexed.REVISION}/{path}"
            ] = raw
        rows.append(
            {
                "annotation_index": number,
                "scene_id": "env_airsim_26",
                "image_path": image_path,
                "locked_development_role": "train",
                "original_annotation_split": "train",
                "annotation_record_sha256": annotation_sha,
                "candidate_frame_count_from_terminal_id": frames,
                "annotation_action_code_counts": {"0": 1, "9": 2},
                "raw_pose_actions_verified": False,
                "parquet_path": f"traj/{image_path}.parquet",
                "pose_path": f"Image/{image_path}/pose.jsonl",
            }
        )
    monkeypatch.setattr(indexed, "PUBLISHED", tuple(published))
    monkeypatch.setattr(
        indexed,
        "BUDGET",
        indexed.BUDGET | {"exact_payload_bytes": sum(map(len, payloads.values()))},
    )
    index = {
        "contract": "multidetect.openfly-bounded-annotation-index-v1",
        "small_sources_unchanged": True,
        "annotation_stat_unchanged": True,
        "source_bindings_before": [small],
        "source_bindings_after": [small],
        "routes": rows,
    }
    record = write(tmp_path / indexed.INDEX_PATH, index)
    monkeypatch.setattr(indexed, "INDEX_BYTES", record["bytes"])
    monkeypatch.setattr(indexed, "INDEX_SHA", record["sha256"])
    plan = indexed.build_plan()
    plan_path = tmp_path / "configs/training/fixture-indexed-plan.json"
    plan_binding = write(plan_path, plan)
    return SimpleNamespace(
        root=tmp_path,
        index=index,
        plan=plan,
        path=plan_path,
        sha=plan_binding["sha256"],
        client=Client(payloads),
        payloads=payloads,
    )


def replace_index(source, monkeypatch, changed):
    record = write(source.root / indexed.INDEX_PATH, changed)
    monkeypatch.setattr(indexed, "INDEX_BYTES", record["bytes"])
    monkeypatch.setattr(indexed, "INDEX_SHA", record["sha256"])


def test_actual_historical_constants_exact_twelve_paths_total_and_roles():
    assert [row[0] for row in indexed.PUBLISHED] == [143, 150, 153, 154, 148, 144]
    assert all("2025-1-1_" in row[1] for row in indexed.PUBLISHED)
    assert sum(row[4] for row in indexed.PUBLISHED) == 83659661
    assert sum(row[6] for row in indexed.PUBLISHED) == 32575
    assert indexed.BUDGET["exact_payload_bytes"] == 83692236 < 128 * 1024 * 1024
    assert indexed.BUDGET["max_http_responses_total_upper_bound"] == 12 * 6
    assert (
        sum(row[2] for row in indexed.PUBLISHED) == 122
    )  # metadata only, not actual row-count proof
    assert all(len(row[5]) == 64 and len(row[7]) == 40 for row in indexed.PUBLISHED)
    assert indexed.INDEX_SHA == "74af3937f614ee78400a3c42cd4087c01a79d8a8d4fd712c57319e3bade25f1c"


def test_plan_contains_only_exact_train_payload_pairs_and_no_annotation_scan(source):
    plan = source.plan
    assert len(plan["routes"]) == 6 and len(plan["payloads"]) == 12
    assert {row["scene_id"] for row in plan["routes"]} == {"env_airsim_26"}
    assert {row["locked_development_role"] for row in plan["routes"]} == {"train"}
    assert [row["role"] for row in plan["payloads"]] == ["shard", "pose"] * 6
    assert [row["digest_kind"] for row in plan["payloads"]] == ["sha256-lfs", "sha1-git-blob"] * 6
    assert all(
        "/low_short/" in row["repository_path"] and "?" not in row["url"]
        for row in plan["payloads"]
    )
    assert plan["metadata_observation"]["tool_chunk"] == "6a877f"
    assert not any("train.json" in row["path"] for row in plan["sources"])
    assert all(value is False for value in plan["claims"].values())
    assert indexed.build_plan() == plan
    assert not source.client.calls


def test_success_actual_downloader_both_digest_kinds_localsha_and_source_closure(source):
    report = indexed.acquire(source.path, source.sha, client=source.client)
    assert report["native_exit_code"] == 0 and report["all_twelve_payloads_verified"] is True
    assert report["payload_files_verified"] == report["initial_payload_requests_attempted"] == 12
    assert report["verified_payload_bytes"] == indexed.BUDGET["exact_payload_bytes"]
    assert report["small_sources_unchanged"] is True
    assert report["sources_before"] == report["sources_after"]
    assert report["runtime_sources_before"] == report["runtime_sources_after"]
    assert report["client_scope"] == "injected_client_not_HTTP_acceptance"
    assert report["http_responses_observed"] == 24
    for row in report["payloads"]:
        raw = source.payloads[row["url"]]
        file = source.root / row["file"]["path"]
        assert file.read_bytes() == raw
        assert row["file"]["sha256"] == hashlib.sha256(raw).hexdigest()
        assert row["response_chain"] == [
            {"host": "huggingface.co"},
            {"host": "cas-bridge.xethub.hf.co"},
        ]
        assert not file.with_suffix(file.suffix + ".partial").exists()
    assert all(response.closed for response in source.client.responses)
    assert all(value is False for value in report["claims"].values())
    saved = (source.root / indexed.OUTPUT / "report.json").read_bytes()
    assert b"signed.invalid" not in saved and b"secret" not in saved and b"Location" not in saved
    assert indexed.strict_json(saved) == report
    with pytest.raises(FileExistsError):
        indexed.acquire(source.path, source.sha, client=source.client)
    assert len(source.client.calls) == 12


@pytest.mark.parametrize(
    "field,value",
    [
        ("scene_id", "env_airsim_23"),
        ("locked_development_role", "dev-test"),
        ("original_annotation_split", "test"),
        ("image_path", "env_airsim_26/wrong"),
        ("annotation_record_sha256", "f" * 64),
        ("candidate_frame_count_from_terminal_id", 16),
        ("candidate_frame_count_from_terminal_id", True),
        ("parquet_path", "traj/wrong.parquet"),
        ("pose_path", "Image/wrong/pose.jsonl"),
        ("raw_pose_actions_verified", True),
    ],
)
def test_fixed_metadata_semantics_fail_closed_before_network(source, monkeypatch, field, value):
    changed = copy.deepcopy(source.index)
    changed["routes"][0][field] = value
    replace_index(source, monkeypatch, changed)
    with pytest.raises(ValueError):
        indexed.build_plan()
    assert not source.client.calls and not (source.root / indexed.OUTPUT).exists()


@pytest.mark.parametrize(
    "change", ["duplicate", "missing", "source_drift", "closure", "stat", "changed_indexsha"]
)
def test_index_hash_membership_and_small_closure_gate(source, monkeypatch, change):
    changed = copy.deepcopy(source.index)
    if change == "duplicate":
        changed["routes"].append(copy.deepcopy(changed["routes"][0]))
    elif change == "missing":
        changed["routes"].pop(0)
    elif change == "closure":
        changed["small_sources_unchanged"] = False
    elif change == "stat":
        changed["annotation_stat_unchanged"] = False
    elif change == "source_drift":
        (source.root / "index-source.json").write_bytes(b"changed")
    elif change == "changed_indexsha":
        (source.root / indexed.INDEX_PATH).write_bytes(b"{}")
    if change not in ("source_drift", "changed_indexsha"):
        replace_index(source, monkeypatch, changed)
    with pytest.raises(ValueError):
        indexed.build_plan()
    assert not source.client.calls


@pytest.mark.parametrize(
    "change", ["digest", "url_query", "role", "bytes", "order", "extra", "runtime", "sha"]
)
def test_explicit_plan_sha_and_canonical_fixed_plan_gate(source, change):
    if change == "sha":
        sha = "a" * 64
    else:
        plan = copy.deepcopy(source.plan)
        if change == "digest":
            plan["payloads"][0]["digest"] = "a" * 64
        elif change == "url_query":
            plan["payloads"][0]["url"] += "?credential=secret"
        elif change == "role":
            plan["routes"][0]["locked_development_role"] = "dev-test"
        elif change == "bytes":
            plan["payloads"][0]["bytes"] += 1
        elif change == "order":
            plan["payloads"].reverse()
        elif change == "extra":
            plan["claims"]["training_ready"] = True
        elif change == "runtime":
            plan["runtime_sources"] = []
        sha = write(source.path, plan)["sha256"]
    with pytest.raises(ValueError):
        indexed.acquire(source.path, sha, client=source.client)
    assert not source.client.calls and not (source.root / indexed.OUTPUT).exists()


@pytest.mark.parametrize(
    "failure",
    [
        "truncated",
        "oversized",
        "wrongdigest",
        "contentlength",
        "encoding",
        "authorization",
        "status",
        "signed_exception",
    ],
)
def test_stream_failures_preserve_partial_stop_no_retry_and_secret_safe_report(
    source, failure, capsys
):
    first = source.plan["payloads"][0]
    raw = source.payloads[first["url"]]
    if failure == "truncated":
        source.client.transform = lambda response: Response(raw[:-1], headers={})
    elif failure == "oversized":
        source.client.transform = lambda response: Response(raw + b"overflow", headers={})
    elif failure == "wrongdigest":
        source.client.transform = lambda response: Response(b"x" * len(raw))
    elif failure == "contentlength":
        source.client.transform = lambda response: Response(raw, headers={"Content-Length": "9999"})
    elif failure == "encoding":
        source.client.transform = lambda response: Response(
            raw, headers={"Content-Encoding": "gzip"}
        )
    elif failure == "authorization":
        source.client.transform = lambda response: Response(raw, authorization=True)
    elif failure == "status":
        source.client.transform = lambda response: Response(raw, status=403)
    else:

        def fail(_url):
            raise RuntimeError(
                "signed URL https://cdn.invalid/?credential=secret Authorization=password"
            )

        source.client.stream = fail
    report = indexed.acquire(source.path, source.sha, client=source.client)
    assert report["native_exit_code"] == 1 and report["all_twelve_payloads_verified"] is False
    assert (
        report["payload_files_verified"] == 0 and report["initial_payload_requests_attempted"] == 1
    )
    assert report["small_sources_unchanged"] is True
    assert len(source.client.calls) <= 1 and all(
        response.closed for response in source.client.responses
    )
    path = source.root / indexed.OUTPUT / "payload" / first["repository_path"]
    assert not path.exists()
    if failure in {"truncated", "oversized", "wrongdigest"}:
        assert path.with_suffix(path.suffix + ".partial").is_file()
    saved = (source.root / indexed.OUTPUT / "report.json").read_bytes()
    assert b"secret" not in saved and b"password" not in saved and b"cdn.invalid" not in saved
    out = capsys.readouterr()
    assert "secret" not in out.out + out.err


@pytest.mark.parametrize("target", ["index", "plan", "source", "payload", "runtime_path"])
def test_final_identity_drift_rejected_and_completed_outputs_preserved(source, monkeypatch, target):
    triggered = False
    original_runtime = indexed.runtime_sources

    def mutate():
        nonlocal triggered
        if triggered:
            return
        triggered = True
        if target == "index":
            (source.root / indexed.INDEX_PATH).write_bytes(b"{}")
        elif target == "plan":
            source.path.write_bytes(b"{}")
        elif target == "source":
            (source.root / indexed.SOURCE_FILES[0]).write_bytes(b"changed fixture")
        elif target == "runtime_path":
            monkeypatch.setattr(indexed, "runtime_sources", lambda: [])
        else:
            # Mutate the first published payload when the second request starts.
            if len(source.client.calls) == 1:
                triggered = False
                return
            path = (
                source.root
                / indexed.OUTPUT
                / "payload"
                / source.plan["payloads"][0]["repository_path"]
            )
            path.write_bytes(b"mutated fixture payload")

    source.client.after_stream = mutate
    report = indexed.acquire(source.path, source.sha, client=source.client)
    assert report["native_exit_code"] == 1 and report["all_twelve_payloads_verified"] is False
    assert report["payload_files_verified"] == 12
    assert any(row["stage"] == "final_identity_check" for row in report["failures"])
    assert (source.root / indexed.OUTPUT / "report.json").is_file()
    assert len(source.client.calls) == 12
    assert original_runtime()  # listed runtime bytes themselves were not changed


def test_partial_prior_success_and_failed_pose_count_remain_truthful(source):
    second = source.plan["payloads"][1]
    source.payloads[second["url"]] = b"bad same size pose".ljust(second["bytes"], b"x")[
        : second["bytes"]
    ]
    report = indexed.acquire(source.path, source.sha, client=source.client)
    assert report["native_exit_code"] == 1 and report["payload_files_verified"] == 1
    assert report["initial_payload_requests_attempted"] == 2
    assert report["verified_payload_bytes"] == source.plan["payloads"][0]["bytes"]
    assert report["http_response_count_scope"] == "verified_files_only_failed_chain_count_unknown"
    assert all(response.closed for response in source.client.responses)


def test_create_only_plan_and_existing_partial_never_replaced(source):
    with pytest.raises(FileExistsError):
        indexed.create_json(source.path, {})
    output = source.root / indexed.OUTPUT
    output.mkdir(parents=True)
    write(output / "retained.partial", b"retain")
    with pytest.raises(FileExistsError):
        indexed.acquire(source.path, source.sha, client=source.client)
    assert (output / "retained.partial").read_bytes() == b"retain"
    assert not source.client.calls


@pytest.mark.parametrize("relative", ["../escape", "/outside", "artifacts/../escape"])
def test_repository_escape_rejected(source, relative):
    with pytest.raises(ValueError):
        indexed.local_path(relative)


def test_strict_json_duplicates_nonfinite_and_cli_errors_sanitized(source, capsys):
    for raw in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}'):
        with pytest.raises(ValueError):
            indexed.strict_json(raw)
    assert indexed.main(["--plan", str(source.path)]) == 1
    assert indexed.main(["--emit-plan", str(source.path), "--plan-sha256", "secret"]) == 1
    assert "secret" not in capsys.readouterr().err


def test_helper_authentication_fail_before_import(tmp_path, monkeypatch):
    monkeypatch.setattr(indexed, "ROOT", tmp_path)
    write(tmp_path / "scripts/fixture.py", b"fixture source")
    monkeypatch.setattr(indexed, "PINNED_HELPERS", {"fixture": (14, "a" * 64)})
    monkeypatch.setattr(
        indexed.importlib, "import_module", lambda _: pytest.fail("unauthenticated import")
    )
    with pytest.raises(ValueError):
        indexed.helpers()


def test_help_import_has_no_model_schema_or_image_modules_and_no_network():
    root = Path(__file__).resolve().parents[1]
    code = (
        "import sys; from scripts import fetch_openfly_indexed_training_routes as m; "
        "assert not any(n.split('.')[0] in {'torch','transformers','peft','PIL','pyarrow'} "
        "for n in sys.modules); print(m.CONTRACT)"
    )
    completed = subprocess.run(
        [sys.executable, "-B", "-c", code], cwd=root, capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
    assert indexed.CONTRACT in completed.stdout


def test_existing_official_redirect_rules_reject_bad_host_without_get(monkeypatch):
    client = fetch.OfficialClient()
    monkeypatch.setattr(
        client.session, "get", lambda *args, **kwargs: pytest.fail("unsafe host GET")
    )
    try:
        for url in (
            "http://huggingface.co/x",
            "https://attacker.invalid/x",
            "https://user:secret@huggingface.co/x",
            "https://huggingface.co/x#secret",
        ):
            with pytest.raises(ValueError):
                client.stream(url)
    finally:
        client.session.close()
