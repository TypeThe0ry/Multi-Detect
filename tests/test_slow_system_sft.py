from __future__ import annotations

import json
from pathlib import Path

from scripts import build_slow_system_sft as builder
from multidetect.autonomy.onboard_decision import parse_onboard_decision


def test_decision_labels_and_task_templates_are_schema_valid_and_unprivileged():
    for kind, kwargs in (
        ("confirm_target", {"track": "track-1", "reason": "Candidate class and color match the task."}),
        ("reject_track", {"track": "track-2", "reason": "Wrong color: requested red, candidate is blue."}),
        ("search_region", {"region": "center", "reason": "No visible candidate; continue searching."}),
        ("ask_clarify", {"reason": "Task lacks the class and color needed to identify a target."}),
    ):
        assert parse_onboard_decision(builder._decision(kind, **kwargs)).decision == kind
    assert "red vehicle" in builder._task_text(("red", "vehicle"), 0)
    assert any("\u7ea2\u8272" in builder._task_text(("red", "vehicle"), i) for i in (3, 4))


def test_build_dataset_keeps_world_splits_and_labels_true_vs_distractor(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    index = root / "index.json"
    index.write_text(json.dumps({"worlds": [
        {"world_id": "w-train", "split": "train"},
        {"world_id": "w-val", "split": "val"},
        {"world_id": "w-test", "split": "test"},
    ]}), encoding="utf-8", newline="\n")
    frames = []
    for world, split in (("w-train", "train"), ("w-val", "val"), ("w-test", "test")):
        frame = root / f"{world}.png"
        frame.write_bytes(b"fake")
        frames.append((
            "campaign", world, split, frame, (100, 100), ("red", "vehicle"), [
                {"target": {"target_id": "target_true_0"}, "box": (10.0, 10.0, 30.0, 30.0), "class": "vehicle", "color": "red", "range_m": 10.0},
                {"target": {"target_id": "distractor_0"}, "box": (50.0, 50.0, 80.0, 80.0), "class": "rover", "color": "blue", "range_m": 10.0},
            ], {}, "before", {},
        ))

    monkeypatch.setattr(builder, "ROOT", root)
    def relative(path):
        path = Path(path).resolve()
        try:
            return str(path.relative_to(root.resolve())).replace("\\", "/")
        except ValueError:
            return path.name
    monkeypatch.setattr(builder, "_relative", relative)
    monkeypatch.setattr(builder, "_eligible_frames", lambda *args, **kwargs: iter(frames))
    report = builder.build_dataset([], root / "out", index=index)
    assert report["counts"]["train"]["total"] >= 2
    assert report["counts"]["val"]["total"] >= 2
    assert report["counts"]["test"]["total"] >= 2
    seen_worlds = {}
    for split in ("train", "val", "test"):
        rows = [json.loads(line) for line in (root / "out" / f"{split}.jsonl").read_text(encoding="utf-8").splitlines()]
        for row in rows:
            seen_worlds.setdefault(row["world"], split)
            assert seen_worlds[row["world"]] == split
            assert "target_true" not in row["task_text"] + row["memory"]
            parse_onboard_decision(row["assistant_target"])
        decisions = {row["assistant_target"]["decision"] for row in rows}
        assert {"confirm_target", "reject_track"} <= decisions

