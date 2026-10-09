from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "fetch_openfly_navigation_pilot", ROOT / "scripts/fetch_openfly_navigation_pilot.py"
)
assert SPEC and SPEC.loader
pilot = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = pilot
SPEC.loader.exec_module(pilot)


def _annotation():
    return {
        "image_path": "env_airsim_18/astar_data/low_short/episode",
        "gpt_instruction": "Proceed to the building; then stop.",
        "action": [8, 0],
        "index_list": ["20250108_190202_1", "20250108_190202_2"],
        "pos": [[3.0, 0.0, 0.0], [6.0, 0.0, 0.0]],
        "yaw": [0.0, 0.0],
    }


def _pose():
    return [
        {
            "action": {
                "imageid": i,
                "pos": [float(i * 3), 0.0, 0.0],
                "yaw": 0.0,
                "type": "stop" if i == 2 else "go straight",
                "value": 0 if i == 2 else 3,
            }
        }
        for i in range(3)
    ] + [{"aim_landmark": {"position": [9, 0, 0], "type": "building"}}]


def _pose_raw(records=None):
    return "\n".join(json.dumps(record, indent=4) for record in records or _pose()).encode()


def _entry(path, raw, *, lfs=False):
    item = {
        "type": "file",
        "path": path,
        "size": len(raw),
        "oid": hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest(),
    }
    if lfs:
        item["lfs"] = {"size": len(raw), "oid": hashlib.sha256(raw).hexdigest()}
    return item


class Response:
    def __init__(self, raw, headers=None, status=200):
        self.raw = raw
        self.headers = headers or {"Content-Length": str(len(raw))}
        self.status_code = status
        self.closed = False

    def iter_content(self, _size):
        yield self.raw

    def close(self):
        self.closed = True

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP failure")


class Client:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []
        self.trees = {}
        self.code_tree = {}
        self.root_entries = []

    def stream(self, url):
        self.calls.append(url)
        return Response(self.payloads[url]), [{"host": "huggingface.co", "headers": {}}]

    def tree(self, path):
        value = self.trees[path]
        return value, json.dumps(value).encode(), []

    def root_tree(self):
        return self.root_entries, json.dumps(self.root_entries).encode(), []

    def metadata(self, _url):
        return self.code_tree, json.dumps(self.code_tree).encode(), []


def _client(monkeypatch):
    annotation = _annotation()
    annotation_raw = json.dumps([annotation], indent=2).encode()
    monkeypatch.setattr(pilot, "ANNOTATION_BYTES", len(annotation_raw))
    monkeypatch.setattr(pilot, "ANNOTATION_SHA256", hashlib.sha256(annotation_raw).hexdigest())
    client = Client({pilot.dataset_url("Annotation/train.json"): annotation_raw})
    card = b"---\r\nlicense: apache-2.0\r\n---\r\n"
    client.payloads[pilot.dataset_url("README.md")] = card
    client.root_entries = [_entry("README.md", card)]
    client.trees["Annotation"] = [_entry("Annotation/train.json", annotation_raw, lfs=True)]
    prefix = "Image/" + annotation["image_path"]
    pose = _pose_raw()
    client.payloads[pilot.dataset_url(prefix + "/pose.jsonl")] = pose
    tree = [_entry(prefix + "/pose.jsonl", pose)]
    for i in range(3):
        image = io.BytesIO()
        Image.new("RGB", (16, 12), (i, 20, 30)).save(image, format="PNG")
        raw = image.getvalue()
        path = prefix + f"/20250108_190202_{i}.png"
        client.payloads[pilot.dataset_url(path)] = raw
        tree.append(_entry(path, raw, lfs=True))
    client.trees[prefix] = tree
    entries = []
    for path in [*pilot.CODE_PATHS, *pilot.CODE_LICENSE_PATHS, "configs/env_airsim_18.yaml"]:
        raw = b"// fixture pinned code; not actual external source\n"
        client.payloads[
            f"https://raw.githubusercontent.com/{pilot.CODE_REPOSITORY}/{pilot.CODE_REVISION}/{path}"
        ] = raw
        entry = _entry(path, raw)
        entries.append({"path": path, "type": "blob", "size": entry["size"], "sha": entry["oid"]})
    client.code_tree = {"sha": pilot.CODE_REVISION, "truncated": False, "tree": entries}
    return client


def test_full_fixture_import_is_train_planned_only_create_only(monkeypatch, tmp_path):
    client = _client(monkeypatch)
    out = tmp_path / "new"
    manifest = pilot.fetch_pilot(out, episodes=1, client=client)
    assert manifest["selection"]["raw_rgb_count"] == 3
    assert manifest["selection"]["annotation_record_count"] == 1
    assert manifest["source_planned_not_executed"] is True
    assert [x["repository_path"] for x in manifest["license_sources"]] == ["README.md", "LICENSE"]
    assert all(x["official_digest_kind"] == "sha1-git-blob" for x in manifest["license_sources"])
    for flag in (
        "training_ready",
        "action_labels_normalized",
        "dt_available",
        "flight_execution_evidence",
        "optimizer_performed",
        "weights_downloaded",
        "seen_unseen_downloaded",
    ):
        assert manifest[flag] is False
    episode = manifest["episodes"][0]
    assert (
        episode["annotation_record"]["representation"]
        == "canonical-extracted-not-original-byte-range"
    )
    assert episode["alignment"]["annotation_image_ids"] == [1, 2]
    assert episode["alignment"]["pose_image_ids"] == [0, 1, 2]
    assert episode["alignment"]["aggregate_action_alignment_verified"] is False
    assert episode["alignment"]["dt_s"] is None
    for record in [
        manifest["annotation"],
        episode["pose"],
        *episode["images"],
        *manifest["license_sources"],
    ]:
        raw = Path(record["path"]).read_bytes()
        assert len(raw) == record["bytes"]
        assert hashlib.sha256(raw).hexdigest() == record["sha256"]
    original = (out / "manifest.json").read_bytes()
    with pytest.raises(FileExistsError):
        pilot.fetch_pilot(out, episodes=1, client=client)
    assert (out / "manifest.json").read_bytes() == original


@pytest.mark.parametrize(
    "path",
    [
        "../bad",
        "Image/a/../b",
        "/absolute",
        "Image/a\\b",
        "Image/a%2fb",
        "Image/a:b",
        "Annotation/seen.json",
        "Annotation/unseen.json",
        "model.safetensors",
    ],
)
def test_forbids_unsafe_paths_and_heldout_or_weights(path):
    with pytest.raises(pilot.PilotError):
        pilot.dataset_url(path)


@pytest.mark.parametrize(
    "url",
    [
        "http://huggingface.co/file",
        "https://evil.hf.co/x",
        "https://huggingface.co.evil/x",
        "https://u:p@huggingface.co/x",
        "https://huggingface.co:8000/x",
        "https://huggingface.co/x#fragment",
    ],
)
def test_unapproved_redirect_rejected(url):
    with pytest.raises(pilot.PilotError):
        pilot._allowed_url(url)


@pytest.mark.parametrize(
    "raw", [b"{}", b'[{"x":1,"x":2}]', b'[{"x":NaN}]', b"[{}] garbage", b"[{},]", b"[{", b"[{} {}]"]
)
def test_annotation_stream_rejects_malformed_json(raw, tmp_path):
    path = tmp_path / "annotation.json"
    path.write_bytes(raw)
    with pytest.raises((pilot.PilotError, ValueError)):
        list(pilot.iter_annotation_records(path))


def test_stream_parses_once_across_chunks_with_unicode(tmp_path):
    records = [_annotation(), {"unicode": "目标" * 40000}, _annotation()]
    path = tmp_path / "annotation.json"
    path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    assert list(pilot.iter_annotation_records(path)) == list(enumerate(records))


@pytest.mark.parametrize(
    "mutation",
    [
        "length",
        "duplicate",
        "reverse",
        "nan",
        "bool",
        "future_field",
        "nonterminal",
        "unsafe_index",
    ],
)
def test_annotation_alignment_and_schema_strict(mutation):
    record = _annotation()
    if mutation == "length":
        record["yaw"].pop()
    elif mutation == "duplicate":
        record["index_list"][1] = record["index_list"][0]
    elif mutation == "reverse":
        record["index_list"].reverse()
    elif mutation == "nan":
        record["pos"][0][0] = float("nan")
    elif mutation == "bool":
        record["action"][0] = True
    elif mutation == "future_field":
        record["future_rgb"] = "oracle"
    elif mutation == "nonterminal":
        record["action"][-1] = 1
    elif mutation == "unsafe_index":
        record["index_list"][0] = "../0"
    with pytest.raises(pilot.PilotError):
        pilot.validate_annotation(record)


def test_concatenated_pose_keeps_raw_stop_and_landmark_without_dt():
    actions, landmark = pilot.parse_pose(_pose_raw())
    assert [a["imageid"] for a in actions] == [0, 1, 2]
    assert actions[-1]["type"] == "stop"
    assert "aim_landmark" in landmark
    assert all("dt" not in a and "timestamp" not in a for a in actions)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "duplicate",
        "reverse",
        "nan",
        "not_stop",
        "interior_stop",
        "extra_clock",
        "no_landmark",
    ],
)
def test_raw_pose_fail_closed(mutation):
    records = _pose()
    if mutation == "missing":
        records.pop(1)
    elif mutation == "duplicate":
        records[1]["action"]["imageid"] = 0
    elif mutation == "reverse":
        records[0], records[1] = records[1], records[0]
    elif mutation == "nan":
        records[0]["action"]["yaw"] = float("nan")
    elif mutation == "not_stop":
        records[-2]["action"]["type"] = "go straight"
    elif mutation == "interior_stop":
        records[0]["action"]["type"] = "stop"
    elif mutation == "extra_clock":
        records[0]["action"]["dt"] = 1
    elif mutation == "no_landmark":
        records.pop()
    with pytest.raises(pilot.PilotError):
        pilot.parse_pose(_pose_raw(records))


def test_annotation_pose_mismatch_is_reported_not_repaired(monkeypatch):
    client = _client(monkeypatch)
    ann = _annotation()
    ann["pos"][0][0] = 3.00001
    original = copy.deepcopy(ann)
    actions, _ = pilot.parse_pose(_pose_raw())
    _, images = pilot.episode_metadata(
        client.trees["Image/" + ann["image_path"]], ann["image_path"]
    )
    alignment = pilot.align_episode(ann, actions, images)
    assert alignment["annotation_pose_yaw_exact_match"] is False
    assert alignment["annotation_pose_yaw_mismatches"] == [{"annotation_step": 0, "raw_imageid": 1}]
    assert ann == original


@pytest.mark.parametrize("mutation", ["missing_rgb", "duplicate_rgb", "foreign", "unknown"])
def test_complete_episode_tree_rejects_partial_foreign_or_unknown(monkeypatch, mutation):
    client = _client(monkeypatch)
    ann = _annotation()
    tree = copy.deepcopy(client.trees["Image/" + ann["image_path"]])
    if mutation == "missing_rgb":
        tree.pop(2)
    elif mutation == "duplicate_rgb":
        tree.append(tree[-1])
    elif mutation == "foreign":
        tree[-1]["path"] = "Image/foreign/20250108_190202_2.png"
    elif mutation == "unknown":
        tree[-1]["path"] = "Image/" + ann["image_path"] + "/weights.bin"
    with pytest.raises(pilot.PilotError):
        pilot.episode_metadata(tree, ann["image_path"])


@pytest.mark.parametrize("mutation", ["bytes", "hash", "length", "cap", "encoding"])
def test_download_rejects_corruption_size_and_encoding(tmp_path, mutation):
    raw = b"raw source bytes"
    url = pilot.dataset_url("Annotation/train.json")
    client = Client({url: raw})
    args = {
        "size": len(raw),
        "expected": hashlib.sha256(raw).hexdigest(),
        "digest_kind": "sha256-lfs",
        "cap": len(raw),
    }
    if mutation == "bytes":
        client.payloads[url] += b"unexpected"
    elif mutation == "hash":
        args["expected"] = "0" * 64
    elif mutation == "length":
        client.stream = lambda _url: (Response(raw, {"Content-Length": "1"}), [])
    elif mutation == "cap":
        args["cap"] -= 1
    elif mutation == "encoding":
        client.stream = lambda _url: (Response(raw, {"Content-Encoding": "gzip"}), [])
    with pytest.raises(pilot.PilotError):
        pilot.download_file(client, url, tmp_path / "raw.json", **args)
    assert not (tmp_path / "raw.json").exists()


def test_cache_is_hash_verified_and_network_not_used(tmp_path):
    raw = b"original cache"
    cache = tmp_path / "cache.json"
    cache.write_bytes(raw)
    client = Client({})
    record = pilot.download_file(
        client,
        pilot.dataset_url("Annotation/train.json"),
        tmp_path / "new.json",
        size=len(raw),
        expected=hashlib.sha256(raw).hexdigest(),
        digest_kind="sha256-lfs",
        cap=len(raw),
        cache=cache,
    )
    assert client.calls == []
    assert record["cache_source"]["sha256"] == record["sha256"]
    assert cache.read_bytes() == raw


@pytest.mark.parametrize("limit", ["image_count", "rgb_bytes"])
def test_budget_rejects_before_any_rgb_download(monkeypatch, tmp_path, limit):
    client = _client(monkeypatch)
    monkeypatch.setattr(pilot, "MAX_IMAGES" if limit == "image_count" else "MAX_PILOT_BYTES", 1)
    with pytest.raises(pilot.PilotError, match="budget"):
        pilot.fetch_pilot(tmp_path / "new", episodes=1, client=client)
    assert client.calls == [
        pilot.dataset_url("README.md"),
        pilot.dataset_url("Annotation/train.json"),
    ]
    assert not (tmp_path / "new/manifest.json").exists()


@pytest.mark.parametrize("count", [0, 4, True])
def test_episode_count_cap(count, tmp_path):
    with pytest.raises(pilot.PilotError):
        pilot.fetch_pilot(tmp_path / "new", episodes=count)
    assert not (tmp_path / "new").exists()


def test_original_headers_are_filtered_without_signed_credentials():
    value = pilot._response_headers(
        {
            "X-Repo-Commit": pilot.REVISION,
            "ETag": "original",
            "Location": "https://signed?token=secret",
            "Set-Cookie": "secret",
            "Authorization": "secret",
        }
    )
    assert value == {"x-repo-commit": pilot.REVISION, "etag": "original"}


def test_create_only_writer_preserves_existing_bytes(tmp_path):
    path = tmp_path / "receipt.json"
    path.write_bytes(b"user file")
    with pytest.raises(FileExistsError):
        pilot._create_bytes(path, b"new")
    assert path.read_bytes() == b"user file"


@pytest.mark.parametrize("mutation", ["missing", "digest", "declaration"])
def test_dataset_license_source_required_and_original_bytes_verified(
    monkeypatch, tmp_path, mutation
):
    client = _client(monkeypatch)
    if mutation == "missing":
        client.root_entries.clear()
    elif mutation == "digest":
        client.root_entries[0]["oid"] = "0" * 40
    else:
        raw = b"---\nlicense: invented\n---\n"
        client.payloads[pilot.dataset_url("README.md")] = raw
        client.root_entries = [_entry("README.md", raw)]
    with pytest.raises(pilot.PilotError):
        pilot.fetch_pilot(tmp_path / "new", episodes=1, client=client)
    assert not (tmp_path / "new/manifest.json").exists()
    assert pilot.dataset_url("Annotation/train.json") not in client.calls


def test_code_license_missing_fails_without_training_manifest(monkeypatch, tmp_path):
    client = _client(monkeypatch)
    client.code_tree["tree"] = [x for x in client.code_tree["tree"] if x["path"] != "LICENSE"]
    with pytest.raises(pilot.PilotError, match="license missing"):
        pilot.fetch_pilot(tmp_path / "new", episodes=1, client=client)
    assert not (tmp_path / "new/manifest.json").exists()


def _replace_annotations(monkeypatch, client, records):
    raw = json.dumps(records, ensure_ascii=False).encode()
    monkeypatch.setattr(pilot, "ANNOTATION_BYTES", len(raw))
    monkeypatch.setattr(pilot, "ANNOTATION_SHA256", hashlib.sha256(raw).hexdigest())
    client.payloads[pilot.dataset_url("Annotation/train.json")] = raw
    client.trees["Annotation"] = [_entry("Annotation/train.json", raw, lfs=True)]


def test_original_indices_preserved_scope_not_selected_by_action_or_prompt(monkeypatch, tmp_path):
    client = _client(monkeypatch)
    other = _annotation()
    other["image_path"] = "env_gs_nwpu01/astar_data/low_short/other"
    # Out-of-review renderer is not downloaded even if its label looks useful.
    other["gpt_instruction"] = "Turn left immediately."
    _replace_annotations(monkeypatch, client, [other, _annotation(), _annotation()])
    manifest = pilot.fetch_pilot(tmp_path / "new", episodes=1, client=client)
    assert manifest["selection"]["skipped_non_airsim_records"] == 1
    assert manifest["episodes"][0]["annotation_index"] == 1
    assert manifest["selection"]["metadata_candidates_available"] == 1
    assert manifest["selection"]["metadata_candidates_checked"] == 1


def test_missing_rgb_episode_reported_not_repaired_before_complete_selection(monkeypatch, tmp_path):
    client = _client(monkeypatch)
    missing = _annotation()
    missing["image_path"] = "env_airsim_18/astar_data/low_short/missing"
    prefix = "Image/" + missing["image_path"]
    client.trees[prefix] = [_entry(prefix + "/pose.jsonl", _pose_raw())]
    _replace_annotations(monkeypatch, client, [missing, _annotation()])
    manifest = pilot.fetch_pilot(tmp_path / "new", episodes=1, client=client)
    assert manifest["episodes"][0]["annotation_index"] == 1
    assert manifest["rejected_records"][0]["annotation_index"] == 0
    assert "complete episode" in manifest["rejected_records"][0]["reason"]
    assert all("/missing/" not in url for url in client.calls)
    assert manifest["selection"]["metadata_candidates_checked"] == 2
