import json

import pytest

from scripts.altair import build_dataset as bd


def make(tmp_path, name, n, split_worlds=("w",), crop="none"):
    d = tmp_path / name
    d.mkdir()
    rows = [{"sample_id": f"{name}-{i}", "split": "train", "world": f"{name}-tw{i % 3}"} for i in range(n)]
    (d / "train.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    test = [{"sample_id": f"{name}-t{i}", "split": "test", "world": f"{name}-hold{i}"} for i in range(2)]
    (d / "test.jsonl").write_text("\n".join(json.dumps(r) for r in test))
    (d / "manifest.json").write_text(json.dumps({"crop_policy": crop}))
    return d


def test_exact_ratios_no_oversampling(tmp_path):
    a, b = make(tmp_path, "a", 100), make(tmp_path, "b", 100)
    r = bd.mix([("a", a, 3.0), ("b", b, 1.0)], seed=1)
    assert r["counts"] == {"a": 100, "b": 33}
    assert len(r["splits"]["train"]) == 133


def test_deterministic_and_written(tmp_path):
    a, b = make(tmp_path, "a", 20), make(tmp_path, "b", 20)
    m1 = bd.write(bd.mix([("a", a, 1), ("b", b, 1)], seed=5), tmp_path / "o1")
    m2 = bd.write(bd.mix([("a", a, 1), ("b", b, 1)], seed=5), tmp_path / "o2")
    assert m1["sha256"] == m2["sha256"]
    assert m1["split_sizes"]["test"] == 4


def test_crop_policy_mismatch_rejected(tmp_path):
    a, b = make(tmp_path, "a", 5), make(tmp_path, "b", 5, crop="candidate_zoom_v2")
    with pytest.raises(ValueError, match="crop_policy"):
        bd.mix([("a", a, 1), ("b", b, 1)])


def test_world_leak_rejected(tmp_path):
    a = make(tmp_path, "a", 5)
    rows = [{"sample_id": "x", "split": "test", "world": "a-tw0"}]
    (a / "test.jsonl").write_text(json.dumps(rows[0]))
    with pytest.raises(ValueError, match="leak"):
        bd.mix([("a", a, 1)])


def test_sample_id_collision_rejected(tmp_path):
    a, b = make(tmp_path, "a", 5), make(tmp_path, "b", 5)
    (b / "train.jsonl").write_text((a / "train.jsonl").read_text())
    with pytest.raises(ValueError, match="collision"):
        bd.mix([("a", a, 1), ("b", b, 1)])


def test_parse_source():
    assert bd.parse_source("p=/x/y:0.4")[0:2] == ("p", __import__("pathlib").Path("/x/y"))
    with pytest.raises(ValueError):
        bd.parse_source("bad")


def test_fixed_total_repeats_small_source(tmp_path):
    big, small = make(tmp_path, "big", 100), make(tmp_path, "small", 10)
    r = bd.mix([("big", big, 0.5), ("small", small, 0.5)], seed=3, total=100)
    assert r["counts"] == {"big": 50, "small": 50}
    ids = [row["sample_id"] for row in r["splits"]["train"]]
    assert len(ids) == len(set(ids)) == 100
    assert sum("#dup" in i for i in ids) == 40


def test_prefix_ids_avoid_collision(tmp_path):
    a, b = make(tmp_path, "a", 5), make(tmp_path, "b", 5)
    (b / "train.jsonl").write_text((a / "train.jsonl").read_text())
    r = bd.mix([("a", a, 1), ("b", b, 1)], prefix_ids=True)
    ids = {row["sample_id"] for row in r["splits"]["train"]}
    assert len(ids) == 10 and "a:a-0" in ids and "b:a-0" in ids


def test_crop_policy_override(tmp_path):
    a, b = make(tmp_path, "a", 5), make(tmp_path, "b", 5, crop="candidate_zoom_v2")
    assert bd.mix([("a", a, 1), ("b", b, 1)], crop_policy="candidate_zoom_v2")["crop_policy"] == "candidate_zoom_v2"


def test_leak_scope_same_source(tmp_path):
    a, b = make(tmp_path, "a", 5), make(tmp_path, "b", 5)
    (b / "test.jsonl").write_text(json.dumps({"sample_id": "x", "split": "test", "world": "a-tw0"}))
    with pytest.raises(ValueError, match="leak"):
        bd.mix([("a", a, 1), ("b", b, 1)])
    bd.mix([("a", a, 1), ("b", b, 1)], leak_scope="same-source")
    # a source's own test worlds still may not appear in its own train
    (b / "test.jsonl").write_text(json.dumps({"sample_id": "x", "split": "test", "world": "b-tw0"}))
    with pytest.raises(ValueError, match="leak"):
        bd.mix([("a", a, 1), ("b", b, 1)], leak_scope="same-source")


def test_on_leak_drop_removes_conflicting_heldout_rows(tmp_path):
    a = make(tmp_path, "a", 5)
    rows = [{"sample_id": "keep", "split": "val", "world": "a-heldout"},
            {"sample_id": "drop1", "split": "val", "world": "a-tw0"},
            {"sample_id": "drop2", "split": "val", "world": "a-tw0"}]
    (a / "val.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    with pytest.raises(ValueError, match="leak"):
        bd.mix([("a", a, 1)])
    r = bd.mix([("a", a, 1)], on_leak="drop")
    assert [row["sample_id"] for row in r["splits"]["val"]] == ["keep"]
    assert r["dropped_heldout"]["val:a"]["rows"] == 2
    manifest = bd.write(r, tmp_path / "o")
    assert manifest["dropped_heldout"]["val:a"]["worlds"] == ["a-tw0"]
