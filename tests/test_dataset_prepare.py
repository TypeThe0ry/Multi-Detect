from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile

from multidetect.dataset_prepare import (
    prepare_dfire_archive,
    remap_and_repair_yolo_labels,
    scenario_repeat_decision,
)


def test_remap_and_repair_swaps_classes_and_clips_partial_box() -> None:
    repaired, changes = remap_and_repair_yolo_labels(
        "0 0.5 0.5 0.2 0.2\n1 0.98 0.5 0.2 0.4\n",
        class_map={0: 1, 1: 0},
    )

    assert repaired.splitlines() == [
        "1 0.5 0.5 0.2 0.2",
        "0 0.94 0.5 0.12 0.4",
    ]
    assert changes[0].action == "remapped"
    assert changes[1].action == "clipped_and_remapped"


def test_remap_and_repair_drops_unusable_box() -> None:
    repaired, changes = remap_and_repair_yolo_labels(
        "1 1.5 0.5 0.2 0.2\n",
        class_map={0: 1, 1: 0},
    )

    assert repaired == ""
    assert changes[0].action == "dropped"
    assert changes[0].reason == "box_outside_image"


def test_prepare_dfire_archive_preserves_split_and_writes_audit(tmp_path: Path) -> None:
    archive = tmp_path / "dfire.zip"
    with ZipFile(archive, "w") as bundle:
        for split in ("train", "test"):
            bundle.writestr(f"{split}/images/a.jpg", b"image")
            bundle.writestr(f"{split}/labels/a.txt", "0 0.5 0.5 0.2 0.2\n")
    out_dir = tmp_path / "prepared"

    report = prepare_dfire_archive(archive, out_dir, extract_images=False)

    assert report["source_class_map"] == {"0": "smoke", "1": "fire"}
    assert report["output_class_map"] == {"0": "fire", "1": "smoke"}
    assert (out_dir / "labels/train/a.txt").read_text(encoding="utf-8") == ("1 0.5 0.5 0.2 0.2\n")
    assert not (out_dir / "images").exists()
    assert (out_dir / "preparation-report.json").is_file()


def test_scenario_repeat_balances_only_difficult_positive_images() -> None:
    dark_small = scenario_repeat_decision(
        "0 0.5 0.5 0.1 0.1\n",
        mean_brightness=40.0,
        maximum_repeat=3,
    )
    background = scenario_repeat_decision("", mean_brightness=20.0, maximum_repeat=3)

    assert dark_small.repeat == 3
    assert dark_small.reasons == ("dark_positive", "small_positive")
    assert background.repeat == 1
    assert background.reasons == ()


def test_scenario_repeat_caps_oversampling() -> None:
    decision = scenario_repeat_decision(
        "1 0.5 0.5 0.05 0.05\n",
        mean_brightness=10.0,
        maximum_repeat=2,
    )

    assert decision.repeat == 2
