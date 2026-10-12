from __future__ import annotations

import hashlib
import json
import random
from collections import Counter
from pathlib import Path

from PIL import Image

from multidetect.autonomy.onboard_decision import parse_onboard_decision
from scripts import build_slow_system_sft as builder
from scripts import slow_system_runtime as runtime


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


def test_fuel_worlds_build_natural_bilingual_tasks_and_focused_decisions(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    truth_dir = root / "configs/scenarios/generated-v2"
    truth_dir.mkdir(parents=True)
    campaign = root / "campaign"
    seed = 20261011
    task = "\u627e\u5230\u7ea2\u8272\u8f7f\u8f66\u5e76\u8bb0\u5f55\u4f4d\u7f6e"
    task_en = "Find the red car and record its position."
    worlds = [
        {"world_id": "gen_fuel_train", "split": "train", "asset_mode": "boxes"},
        {"world_id": "gen_fuel_val", "split": "val", "asset_mode": "fuel_v1"},
        {"world_id": "gen_fuel_test", "split": "test"},
    ]
    for world in worlds:
        world.update(task_text=task, task_text_en=task_en)
    index = root / "index.json"
    index.write_text(json.dumps({"asset_mode": "fuel_v1", "worlds": worlds}), encoding="utf-8", newline="\n")
    expected = {}
    for world_number, world in enumerate(worlds):
        world_id = world["world_id"]
        targets = [
            {"target_id": "mesh-object-17", "true_target": True, "asset_slug": "red_sedan",
             "class": "car", "category_words": ["car", "sedan"], "color": "red",
             "size_m": [4.2, 1.8, 1.4], "bottom_offset_m": -0.7},
            {"target_id": "target_true_impostor", "true_target": False, "asset_slug": "blue_hatchback",
             "class": "car", "category_words": ["hatchback", "car"], "color": "blue",
             "size_m": [3.8, 1.7, 1.3], "bottom_offset_m": -0.65},
        ]
        truth = {"targets": targets}
        if world_number == 0:
            truth["asset_mode"] = "fuel_v1"
        (truth_dir / f"{world_id}.truth.json").write_text(json.dumps(truth), encoding="utf-8", newline="\n")
        episode = campaign / world_id / "episode-1"
        episode.mkdir(parents=True)
        metadata = {"status": "complete"}
        if world_number != 2:
            metadata["task_text"] = task
        (episode / "episode.json").write_text(json.dumps(metadata), encoding="utf-8", newline="\n")
        steps = []
        for case_number, case in enumerate(("close", "far", "small", "teacher", "search")):
            for suffix in range(10000):
                name = f"{case}-{suffix}.png"
                variant = int(hashlib.sha256(f"{world_id}:{name}:{seed}".encode()).hexdigest()[:8], 16)
                if (case == "close" and variant % 70 == 0) or (case != "close" and variant % 2 == 1):
                    break
            else:
                raise AssertionError("Could not find deterministic bilingual/clarification frame names")
            image = Image.new("RGB", (96, 80))
            rng = random.Random(world_number * 100 + case_number)
            image.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256)) for _ in range(96 * 80)])
            image.save(episode / name)
            observation = {"rgb_path": name, "fixture_case": case}
            side = "after" if case == "teacher" else "before"
            steps.append({
                f"obs_{side}": observation,
                "labels": {"phase": "approach", "target_range_m": 65, "target_in_fov": True},
            })
            expected[(world_id, name)] = "search_region" if case == "search" else (
                "confirm_target" if case == "close" else "approach_track"
            )
        (episode / "steps.jsonl").write_text(
            "".join(json.dumps(step) + "\n" for step in steps), encoding="utf-8", newline="\n",
        )

    def project(obs, targets, sizes, width, height, obstacles, **kwargs):
        case = obs["fixture_case"]
        if case == "search":
            return []
        return [
            {"target": targets[0], "box": (4.0, 4.0, 14.0, 14.0) if case == "small" else (4.0, 4.0, 24.0, 24.0),
             "class": "car", "color": "red", "range_m": 60.0 if case == "far" else 10.0, "visible_fraction": 1.0},
            {"target": targets[1], "box": (60.0, 40.0, 85.0, 65.0),
             "class": "car", "color": "blue", "range_m": 10.0, "visible_fraction": 0.8},
        ]

    monkeypatch.setattr(builder, "ROOT", root)
    monkeypatch.setattr(runtime, "ROOT", root)
    monkeypatch.setattr(builder, "_relative", lambda path: Path(path).resolve().relative_to(root.resolve()).as_posix()
                        if Path(path).resolve().is_relative_to(root.resolve()) else Path(path).name)
    monkeypatch.setattr(builder, "_sdf_sizes", lambda path: {})
    monkeypatch.setattr(builder, "load_sdf_obstacles", lambda path: [])
    monkeypatch.setattr(builder, "_project", project)
    output = root / "out"
    report = builder.build_dataset([campaign], output, index=index, seed=seed, crop_policy="candidate_zoom_v2")
    assert report["source_counts"].get("missing_task_slug", 0) == 0
    assert report["source_counts"]["eligible_frames"] == 15
    for split in ("train", "val", "test"):
        rows = [json.loads(line) for line in (output / f"{split}.jsonl").read_text(encoding="utf-8").splitlines()]
        assert {row["assistant_target"]["decision"] for row in rows} == {
            "confirm_target", "approach_track", "reject_track", "search_region", "ask_clarify",
        }
        assert {row["task_text"] for row in rows if row["label_type"] != "ambiguous_task"} == {task, task_en}
        for row in rows:
            decision = parse_onboard_decision(row["assistant_target"]).decision
            assert len(row["assistant_target"]["reason"].split()) <= 20
            if row["label_type"] == "true_target":
                assert decision == expected[(row["world"], Path(row["image_path"]).name)]
            elif row["label_type"] == "distractor":
                assert decision == "reject_track"
                assert "blue" in row["assistant_target"]["reason"]
                assert "red" in row["assistant_target"]["reason"]
            elif row["label_type"] == "ambiguous_task":
                assert row["task_text"] in {"\u627e\u5230\u76ee\u6807\u5e76\u8bb0\u5f55\u4f4d\u7f6e", "Find the target."}
            if row["candidates"] and decision != "ask_clarify":
                assert row["assistant_target"]["args"]["track_id"] == row["focus_track_id"]
            prompt = runtime.chat_messages(row, crop_policy="candidate_zoom_v2")[0]["content"][0]["text"]
            for privileged in ("mesh-object-17", "target_true_impostor", "red_sedan", "blue_hatchback",
                               "true_target", "asset_slug", "category_words", "bottom_offset_m"):
                assert privileged not in prompt
            assert row["assistant_target"]["reason"] not in prompt


def test_fuel_projection_uses_measured_geometry_and_filters_occlusion(monkeypatch):
    projection_calls = []
    visibility_calls = []

    def project(**kwargs):
        projection_calls.append(kwargs)
        return (10.0, 10.0, 30.0, 30.0)

    def visibility(**kwargs):
        visibility_calls.append(kwargs)
        return 0.2 if kwargs["target_ned"][0] == 20.0 else 0.8

    monkeypatch.setattr(builder, "project_target_bbox", project)
    monkeypatch.setattr(builder, "visible_fraction", visibility)
    targets = [
        {"target_id": f"mesh-{north}", "true_target": north == 10, "class": "car", "color": "red", "size_m": [4.2, 1.8, 1.4],
         "bottom_offset_m": -0.7, "pose": {"y_north_m": north, "x_east_m": 3, "z_up_m": 0, "yaw_rad": 0.6}}
        for north in (10, 20)
    ]
    stats = Counter()
    obstacles = [object()]
    projected = builder._project(
        {"pose": {"local_ned_m": [0, 0, -5]}, "camera_info": {"hfov_rad": 1.0}},
        targets, {"mesh-10": (99, 99, 99)}, 64, 64, obstacles, min_visible_fraction=0.3, stats=stats,
    )
    assert len(projected) == 1 and projected[0]["target"]["target_id"] == "mesh-10"
    assert projected[0]["color"] == "red"
    assert stats["dropped_occluded"] == 1
    assert len(projection_calls) == len(visibility_calls) == 2
    for call in projection_calls + visibility_calls:
        assert call["target_size_m"] == (4.2, 1.8, 1.4)
        assert call["target_yaw_rad"] == 0.6
        assert call["target_bottom_offset_m"] == -0.7
    assert all(call["obstacles"] is obstacles for call in visibility_calls)



def test_fuel_rejection_does_not_invent_a_task_color():
    candidate = {"category_words": ["hatchback", "car"], "color": "blue"}
    requested = {"category_words": ["car", "sedan"], "color": "red"}
    generic = builder._fuel_reject_reason(candidate, requested, "Find the car.")
    assert "blue" in generic and "red" not in generic
    specific = builder._fuel_reject_reason(candidate, requested, "Find the red car.")
    assert "blue" in specific and "red" in specific
    chinese = builder._fuel_reject_reason(candidate, requested, "\u627e\u5230\u7ea2\u8272\u8f7f\u8f66")
    assert "red" in chinese
    assert max(len(reason.split()) for reason in (generic, specific, chinese)) <= 20
