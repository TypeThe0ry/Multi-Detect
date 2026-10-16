from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from scripts import build_slow_system_sft as builder
from scripts.rebalance_sft_dataset import rebalance_dataset
from scripts.sft_task_category_balance import balance_task_categories


def _row(sample_id: str, category: str, decision: str) -> dict:
    return {
        "sample_id": sample_id,
        "task_category": category,
        "assistant_target": {"decision": decision},
    }


def test_balance_is_deterministic_capped_and_only_duplicates_positives() -> None:
    rows = (
        [_row(f"p{i}", "backpack", "confirm_target") for i in range(2)]
        + [_row(f"n{i}", "backpack", "reject_track") for i in range(10)]
        + [_row("p0-other", "mailbox", "approach_track")]
    )
    first, report = balance_task_categories(rows, min_ratio=0.6, max_upsample=3, seed=7)
    second, _ = balance_task_categories(rows, min_ratio=0.6, max_upsample=3, seed=7)
    assert first == second
    assert report["backpack"] == {
        "positive_before": 2, "negative_before": 10,
        "positive_after": 6, "negative_after": 10, "duplicates_added": 4,
    }
    assert sum(
        row["assistant_target"]["decision"] in {"confirm_target", "approach_track"}
        for row in first if row["task_category"] == "backpack"
    ) == 6
    assert all(
        "~task-balance-" in row["sample_id"]
        for row in first if row["sample_id"].startswith("p") and "~" in row["sample_id"]
    )
    assert len(first) == len(rows) + 4


def test_standalone_copies_val_test_bytes_and_writes_manifest(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    rows = [_row("p", "backpack", "confirm_target")] + [
        _row(f"n{i}", "backpack", "reject_track") for i in range(4)
    ]
    (input_dir / "train.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    val_bytes = b"val\r\nbytes\n"
    test_bytes = b"test\x00bytes\r\n"
    (input_dir / "val.jsonl").write_bytes(val_bytes)
    (input_dir / "test.jsonl").write_bytes(test_bytes)
    (input_dir / "manifest.json").write_text("{}\n", encoding="utf-8")
    report = rebalance_dataset(
        input_dir, output_dir, min_ratio=0.6, max_upsample=4, seed=3, repo_root=tmp_path
    )
    assert report["categories"]["backpack"]["positive_after"] == 3
    assert (output_dir / "val.jsonl").read_bytes() == val_bytes
    assert (output_dir / "test.jsonl").read_bytes() == test_bytes
    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["task_category_balance"]["rows_after"] == 7


def test_builder_default_balance_off_is_byte_identical(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    index = root / "index.json"
    worlds = [{"world_id": f"w-{split}", "split": split} for split in ("train", "val", "test")]
    index.write_text(json.dumps({"worlds": worlds}), encoding="utf-8")
    frame = root / "frame.png"
    frame.write_bytes(b"fixture")
    projected = [
        {
            "target": {"target_id": "target_true_0"}, "box": (1.0, 1.0, 30.0, 30.0),
            "class": "vehicle", "color": "red", "range_m": 1.0,
        },
        {
            "target": {"target_id": "distractor_0"}, "box": (40.0, 40.0, 70.0, 70.0),
            "class": "rover", "color": "blue", "range_m": 1.0,
        },
    ]

    monkeypatch.setattr(builder, "ROOT", root)
    def relative(path: Path) -> str:
        resolved = Path(path).resolve()
        return (
            resolved.relative_to(root.resolve()).as_posix()
            if resolved.is_relative_to(root.resolve()) else resolved.name
        )

    monkeypatch.setattr(builder, "_relative", relative)

    def frames(*args, **kwargs):
        for split in ("train", "val", "test"):
            yield (
                "campaign", f"w-{split}", split, frame, (100, 100), ("red", "vehicle"),
                projected, {}, "before", Counter(), None,
            )

    monkeypatch.setattr(builder, "_eligible_frames", frames)
    first = builder.build_dataset([], root / "out1", index=index)
    builder.build_dataset([], root / "out2", index=index)
    for name in ("train.jsonl", "val.jsonl", "test.jsonl", "manifest.json"):
        assert (root / "out1" / name).read_bytes() == (root / "out2" / name).read_bytes()
    assert "task_category_balance" not in first
    first_row = json.loads(
        (root / "out1" / "train.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    assert "task_category" not in first_row

    builder.build_dataset(
        [], root / "out-balanced", index=index, balance_task_categories=0.6, max_upsample=4,
    )
    for split in ("val", "test"):
        assert (root / "out1" / f"{split}.jsonl").read_bytes() == (
            root / "out-balanced" / f"{split}.jsonl"
        ).read_bytes()
