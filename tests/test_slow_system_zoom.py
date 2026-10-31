from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from multidetect.autonomy.onboard_decision import DECISION_JSON_SCHEMA
from scripts import train_slow_system_lora as trainer
from scripts.slow_system_runtime import (
    candidate_crop,
    chat_messages,
    dataset_crop_policy,
    image_list_for_row,
    trained_key_order_schema,
)


def _row(path: Path, box: list[float], decision: str = "confirm_target") -> dict:
    return {
        "image_path": str(path),
        "task_text": "Find the red vehicle.",
        "memory": "none",
        "candidates": [{"track_id": "track-1", "box_xyxy_normalized": box}],
        "assistant_target": {"decision": decision, "args": {"track_id": "track-1"}},
    }


def test_candidate_crop_is_square_and_pads_border(tmp_path: Path) -> None:
    source = Image.new("RGB", (100, 80), (10, 20, 30))
    path = tmp_path / "frame.png"
    source.save(path)
    crop = candidate_crop(source, _row(path, [0.9, 0.8, 1.0, 1.0]))
    assert crop is not None and crop.size == (224, 224)
    assert crop.getpixel((223, 223)) == (128, 128, 128)


def test_old_manifest_is_single_image_and_new_policy_adds_crop(tmp_path: Path) -> None:
    path = tmp_path / "frame.png"
    Image.new("RGB", (100, 100)).save(path)
    row = _row(path, [0.4, 0.4, 0.5, 0.5])
    assert len(chat_messages(row)) == 1
    assert len(chat_messages(row, crop_policy="candidate_zoom_v1")[0]["content"]) == 3
    (tmp_path / "manifest.json").write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    assert dataset_crop_policy(tmp_path) == "none"


def test_balance_rows_is_deterministic_and_within_two_to_one() -> None:
    rows = []
    for decision, count in (("confirm_target", 1), ("approach_track", 2), ("reject_track", 8)):
        rows.extend(
            {"assistant_target": {"decision": decision}, "sample_id": f"{decision}-{i}"}
            for i in range(count)
        )
    first = trainer.balance_rows(rows, seed=7)
    second = trainer.balance_rows(rows, seed=7)
    assert [r["sample_id"] for r in first] == [r["sample_id"] for r in second]
    counts = {
        name: sum(r["assistant_target"]["decision"] == name for r in first)
        for name in ("confirm_target", "approach_track", "reject_track")
    }
    assert max(counts.values()) <= 2 * min(counts.values())


def test_zoom_v2_crops_every_candidate_without_reading_the_label(tmp_path: Path) -> None:
    path = tmp_path / "frame.png"
    image = Image.new("RGB", (200, 100), (0, 0, 0))
    image.paste((255, 0, 0), (20, 20, 40, 40))
    image.paste((0, 0, 255), (150, 50, 170, 70))
    image.save(path)
    row = _row(path, [0.1, 0.2, 0.2, 0.4])
    row["candidates"].append({"track_id": "track-2", "box_xyxy_normalized": [0.75, 0.5, 0.85, 0.7]})
    relabelled = {"decision": "reject_track", "args": {"track_id": "track-2"}}
    other = {**row, "assistant_target": relabelled}
    first = image_list_for_row(row, crop_policy="candidate_zoom_v2")
    second = image_list_for_row(other, crop_policy="candidate_zoom_v2")
    assert len(first) == 3
    assert [im.tobytes() for im in first] == [im.tobytes() for im in second]
    assert first[1].getpixel((112, 112))[0] > 200 and first[2].getpixel((112, 112))[2] > 200
    prompt = chat_messages(row, crop_policy="candidate_zoom_v2")[0]["content"][0]["text"]
    assert "image 2=track-1" in prompt and "image 3=track-2" in prompt


def test_llama_schema_property_order_matches_trained_serialisation() -> None:
    schema = trained_key_order_schema(DECISION_JSON_SCHEMA)
    args = {"track_id": "t", "region": None, "duration_s": None}
    target = {"decision": "approach_track", "reason": "x", "args": args}
    serialised = json.loads(json.dumps(target, sort_keys=True))
    assert list(schema["properties"]) == list(serialised)
    assert list(schema["properties"]["args"]["properties"]) == list(serialised["args"])


def _llama_property_orders(schema: object):
    """Yield (sorted, emitted) property orders; llama.cpp emits required before optional."""
    if isinstance(schema, dict):
        if isinstance(schema.get("properties"), dict):
            names = list(schema["properties"])
            required = set(schema.get("required", []))
            emitted = [n for n in names if n in required] + [n for n in names if n not in required]
            yield names, emitted
        for value in schema.values():
            yield from _llama_property_orders(value)
    elif isinstance(schema, list):
        for value in schema:
            yield from _llama_property_orders(value)


def test_plan_schema_grammar_order_matches_sorted_keys() -> None:
    from multidetect.autonomy.mission_planner import plan_json_schema

    schema = trained_key_order_schema(plan_json_schema())
    for names, emitted in _llama_property_orders(schema):
        assert names == sorted(names)
        assert emitted == names
    count = next(
        step
        for step in schema["anyOf"][0]["properties"]["steps"]["items"]["anyOf"]
        if step["properties"]["skill"]["enum"] == ["count"]
    )
    branches = count["properties"]["params"]["anyOf"]
    orders = [list(branch["properties"]) for branch in branches]
    assert ["max_count", "query", "region"] in orders
    assert ["query", "region"] in orders
    # Every branch keeps the required parameters.
    assert all({"query", "region"} <= set(branch["required"]) for branch in branches)


def test_zoom_v2_multi_candidate_prompt_names_the_event_focus(tmp_path: Path) -> None:
    path = tmp_path / "frame.png"
    Image.new("RGB", (200, 100)).save(path)
    row = _row(path, [0.1, 0.2, 0.2, 0.4])
    row["candidates"].append({"track_id": "track-2", "box_xyxy_normalized": [0.7, 0.5, 0.8, 0.7]})
    row["focus_track_id"] = "track-2"
    text = chat_messages(row, crop_policy="candidate_zoom_v2")[0]["content"][0]["text"]
    assert "focus track_id=track-2" in text
    assert "Pick the matching candidate" not in text
    del row["focus_track_id"]
    text = chat_messages(row, crop_policy="candidate_zoom_v2")[0]["content"][0]["text"]
    assert "Pick the matching candidate" in text
