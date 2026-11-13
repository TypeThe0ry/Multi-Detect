import json

import pytest

from scripts.altair import build_dataset as bd


def make(tmp_path, name, n, crop="none"):
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
    row = {"sample_id": "x", "split": "test", "world": "a-tw0"}
    (a / "test.jsonl").write_text(json.dumps(row))
    with pytest.raises(ValueError, match="leak"):
        bd.mix([("a", a, 1)])


def test_sample_id_collision_rejected(tmp_path):
    a, b = make(tmp_path, "a", 5), make(tmp_path, "b", 5)
    (b / "train.jsonl").write_text((a / "train.jsonl").read_text())
    with pytest.raises(ValueError, match="collision"):
        bd.mix([("a", a, 1), ("b", b, 1)])


def test_parse_source():
    from pathlib import Path

    assert bd.parse_source("p=/x/y:0.4")[0:2] == ("p", Path("/x/y"))
    with pytest.raises(ValueError):
        bd.parse_source("bad")
