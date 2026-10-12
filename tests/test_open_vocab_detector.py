import hashlib
from pathlib import Path

import pytest
import yaml

from scripts.open_vocab_detector import (
    build_dataset_view,
    load_dataset,
    prompt_for,
    score_predictions,
)


def make_dataset(root: Path) -> Path:
    names = ["vehicle", "stop_sign", "suv", "pickup", "postbox", "traffic_cone", "car"]
    for split in ("train", "val", "test"):
        (root / "images" / split).mkdir(parents=True)
        (root / "labels" / split).mkdir(parents=True)
        for stem in ("mixed", "heldout", "empty"):
            (root / "images" / split / f"{stem}.jpg").write_bytes(b"synthetic image")
        (root / "labels" / split / "mixed.txt").write_text(
            "0 0.2 0.2 0.1 0.1\n1 0.4 0.4 0.1 0.1\n2 0.6 0.6 0.1 0.1\n5 0.8 0.8 0.1 0.1\n",
            encoding="utf-8",
        )
        (root / "labels" / split / "heldout.txt").write_text(
            "3 0.3 0.3 0.1 0.1\n4 0.7 0.7 0.1 0.1\n", encoding="utf-8"
        )
        (root / "labels" / split / "empty.txt").write_text("", encoding="utf-8")
    data = root / "data.yaml"
    data.write_text(
        yaml.safe_dump(
            {
                "path": str(root),
                "train": "images/train",
                "val": "images/val",
                "test": "images/test",
                "names": names,
            }
        ),
        encoding="utf-8",
    )
    return data


def test_holdout_filter_remaps_and_preserves_background_images(tmp_path):
    data = make_dataset(tmp_path / "source")
    originals = {path: path.read_bytes() for path in data.parent.rglob("*") if path.is_file()}
    output = tmp_path / "derived"
    manifest = build_dataset_view(data, output, ["stop_sign", "postbox", "pickup"])
    dataset = output / "dataset"
    names, splits = load_dataset(dataset / "data.yaml")
    assert names == ["vehicle", "suv", "traffic_cone"]
    assert manifest["excluded_unobserved_classes"] == ["car"]
    for split in ("train", "val"):
        assert len(splits[split]) == 3
        labels = sorted((dataset / "labels" / split).glob("*.txt"))
        assert len(labels) == 3
        nonempty = [path.read_text(encoding="utf-8") for path in labels if path.stat().st_size]
        assert len(nonempty) == 1
        rows = [row.split() for row in nonempty[0].splitlines()]
        assert [int(row[0]) for row in rows] == [0, 1, 2]
        assert [row[1:] for row in rows] == [
            ["0.2", "0.2", "0.1", "0.1"],
            ["0.6", "0.6", "0.1", "0.1"],
            ["0.8", "0.8", "0.1", "0.1"],
        ]
        counts = manifest["splits"][split]
        assert counts["images"] == 3
        assert counts["source_counts"]["stop_sign"] == 1
        assert counts["retained_counts"]["suv"] == 1
        assert counts["removed_counts"]["pickup"] == 1
        assert sum(counts["retained_counts"].values()) == 3
        assert sum(counts["removed_counts"].values()) == 3
    assert manifest["splits"]["test"]["source_counts"]["postbox"] == 1
    assert manifest["splits"]["test"]["retained_counts"]["postbox"] == 1
    assert sum(manifest["splits"]["test"]["removed_counts"].values()) == 0
    assert manifest["holdout_classes"] == ["stop_sign", "postbox", "pickup"]
    for path, content in originals.items():
        assert manifest["source_hashes"][str(path.resolve())] == hashlib.sha256(content).hexdigest()
    assert {path: path.read_bytes() for path in originals} == originals


@pytest.mark.parametrize(
    "name,expected",
    [
        ("vehicle", "car"),
        ("suv", "SUV"),
        ("pickup", "pickup truck"),
        ("fire_truck", "fire truck"),
        ("traffic_cone", "traffic cone"),
        ("jersey_barrier", "concrete barrier"),
        ("fire_hydrant", "fire hydrant"),
        ("electrical_box", "electrical box"),
        ("cardboard_box", "cardboard box"),
        ("stop_sign", "stop sign"),
        ("postbox", "post box"),
        ("unlisted_object", "unlisted object"),
    ],
)
def test_text_prompts(name, expected):
    assert prompt_for(name) == expected


def box(name, coords=(0, 0, 10, 10), confidence=None):
    result = {"class": name, "box": list(coords)}
    if confidence is not None:
        result["confidence"] = confidence
    return result


def test_class_aware_matching_counts_duplicates_and_wrong_classes():
    truth = [[box("vehicle"), box("stop_sign", (20, 20, 30, 30))]]
    predictions = [
        [
            box("vehicle", confidence=0.9),
            box("vehicle", confidence=0.8),
            box("vehicle", (20, 20, 30, 30), confidence=0.7),
            box("stop_sign", (40, 40, 50, 50), confidence=0.6),
        ]
    ]
    result = score_predictions(truth, predictions, ["vehicle", "stop_sign"], ["stop_sign"])
    car = result["per_class"]["vehicle"]
    assert (car["gt"], car["tp"], car["fp"]) == (1, 1, 2)
    assert car["precision"] == pytest.approx(1 / 3)
    assert car["recall"] == 1
    assert car["ap50"] == pytest.approx(1, abs=0.01)
    sign = result["per_class"]["stop_sign"]
    assert (sign["gt"], sign["tp"], sign["fp"]) == (1, 0, 1)
    assert sign["ap50"] == 0
    assert result["aggregates"]["seen"]["map50"] == pytest.approx(1, abs=0.01)
    assert result["aggregates"]["holdout"]["map50"] == 0
    assert result["aggregates"]["all"]["map50"] == pytest.approx(0.5, abs=0.01)


def test_ap_uses_global_confidence_order_and_below_threshold_detections():
    truth = [[box("vehicle")], [box("vehicle")]]
    predictions = [
        [box("vehicle", confidence=0.8)],
        [box("vehicle", (20, 20, 30, 30), confidence=0.9), box("vehicle", confidence=0.1)],
    ]
    result = score_predictions(truth, predictions, ["vehicle"], [], confidence=0.25)
    car = result["per_class"]["vehicle"]
    assert (car["gt"], car["tp"], car["fp"]) == (2, 1, 1)
    assert car["precision"] == 0.5
    assert car["recall"] == 0.5
    assert car["ap50"] == pytest.approx(2 / 3, abs=0.01)


def test_matching_is_greedy_in_confidence_order_and_obeys_iou_threshold():
    truth = [[box("vehicle")]]
    predictions = [
        [
            box("vehicle", confidence=0.1),
            box("vehicle", (0, 0, 10, 5), confidence=0.9),
            box("vehicle", (0, 0, 10, 4.9), confidence=0.8),
        ]
    ]
    result = score_predictions(truth, predictions, ["vehicle"], [], confidence=0.25)
    car = result["per_class"]["vehicle"]
    assert (car["tp"], car["fp"]) == (1, 1)
    assert car["recall"] == 1
    assert car["ap50"] == pytest.approx(1, abs=0.01)


def test_empty_predictions_and_background_false_positives_are_finite():
    result = score_predictions(
        [[box("vehicle")], []],
        [[], [box("stop_sign", confidence=0.9)]],
        ["vehicle", "stop_sign"],
        ["stop_sign"],
    )
    assert result["per_class"]["vehicle"]["recall"] == 0
    assert result["per_class"]["stop_sign"]["fp"] == 1
    assert result["aggregates"]["all"]["map50"] == 0
