import json

from scripts.altair import rebalance_decisions as rd


def row(i, label, side, as_string=False):
    box = [0.1, 0.1, 0.1 + side, 0.1 + side]
    target = {"args": {"duration_s": None, "region": None, "track_id": "t1"}, "decision": label, "reason": "x"}
    return {"sample_id": f"s{i}", "split": "train", "candidates": [{"track_id": "t1", "box_xyxy_normalized": box}],
            "assistant_target": json.dumps(target) if as_string else target}


def test_area_and_string_target():
    r = row(0, rd.CONFIRM, 0.1, as_string=True)
    assert abs(rd.target_area(r) - 0.01) < 1e-9
    assert rd.decision(r) == rd.CONFIRM


def test_balances_within_bins():
    rows = [row(i, rd.APPROACH, 0.01 + 0.0001 * i) for i in range(60)]
    rows += [row(100 + i, rd.CONFIRM, 0.01 + 0.0001 * i) for i in range(20)]
    rows += [row(200 + i, "reject_track", 0.05) for i in range(10)]
    kept, report = rd.rebalance(rows, bins=4, seed=1)
    c = sum(rd.decision(r) == rd.CONFIRM for r in kept)
    a = sum(rd.decision(r) == rd.APPROACH for r in kept)
    assert c == a
    assert sum(rd.decision(r) == "reject_track" for r in kept) == 10
    assert report["dropped"] == len(rows) - len(kept)


def test_rows_without_box_kept():
    bad = {"sample_id": "x", "assistant_target": {"args": {"track_id": "zz"}, "decision": rd.CONFIRM}, "candidates": []}
    kept, _ = rd.rebalance([bad, row(1, rd.CONFIRM, 0.1)], bins=2)
    assert bad in kept


def test_run_copies_val(tmp_path):
    src, out = tmp_path / "s", tmp_path / "o"
    src.mkdir()
    rows = [row(i, rd.APPROACH, 0.02) for i in range(6)] + [row(10 + i, rd.CONFIRM, 0.02) for i in range(2)]
    (src / "train.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (src / "val.jsonl").write_text("{}\n")
    (src / "manifest.json").write_text(json.dumps({"crop_policy": "candidate_zoom_v2"}))
    report = rd.run(src, out, bins=2, seed=3)
    assert (out / "val.jsonl").read_text() == "{}\n"
    assert json.loads((out / "manifest.json").read_text())["crop_policy"] == "candidate_zoom_v2"
    assert len((out / "train.jsonl").read_text().splitlines()) == 4
    assert report["dropped"] == 4


def test_trim_approach_only_keeps_confirm():
    rows = [row(i, rd.APPROACH, 0.01 + 0.0001 * i) for i in range(60)]
    rows += [row(100 + i, rd.CONFIRM, 0.01 + 0.0001 * i) for i in range(20)]
    kept, _ = rd.rebalance(rows, bins=4, seed=1, trim="approach")
    assert sum(rd.decision(r) == rd.CONFIRM for r in kept) == 20
    assert sum(rd.decision(r) == rd.APPROACH for r in kept) <= 60


def test_per_bin_reports_actual_kept():
    rows = [row(i, rd.APPROACH, 0.01 + 0.0001 * i) for i in range(60)]
    rows += [row(100 + i, rd.CONFIRM, 0.01 + 0.0001 * i) for i in range(20)]
    kept, report = rd.rebalance(rows, bins=4, seed=1, trim="approach")
    assert sum(b["confirm_kept"] for b in report["per_bin"]) == sum(rd.decision(r) == rd.CONFIRM for r in kept)
    assert sum(b["approach_kept"] for b in report["per_bin"]) == sum(rd.decision(r) == rd.APPROACH for r in kept)
