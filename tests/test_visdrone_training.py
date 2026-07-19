from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_visdrone_candidate_is_pinned_and_exports_traditional_raw_onnx() -> None:
    descriptor = json.loads(
        (ROOT / "configs/models/visdrone_yolo26n_priority_candidate.json").read_text(
            encoding="utf-8"
        )
    )
    trainer = (ROOT / "scripts/train_visdrone_priority_candidate.py").read_text(
        encoding="utf-8"
    )

    assert len(descriptor["source_checkpoint_sha256"]) == 64
    assert descriptor["dataset"]["name"] == "VisDrone2019-DET"
    assert descriptor["export_contract"]["embedded_nms"] is False
    assert descriptor["export_contract"]["topk_nodes"] == 0
    assert descriptor["production_approved"] is False
    assert "PINNED_PT_SHA256" in trainer
    assert 'operators.intersection({"TopK", "NonMaxSuppression"})' in trainer
    assert "export_model = YOLO(str(best))" in trainer


def test_visdrone_converter_preserves_train_val_and_runtime_label_map() -> None:
    converter = (ROOT / "scripts/prepare_visdrone_dataset.py").read_text(encoding="utf-8")
    trainer = (ROOT / "scripts/train_visdrone_priority_candidate.py").read_text(
        encoding="utf-8"
    )

    assert '(("train", "train"), ("val", "val"))' in converter
    assert "row[4] == \"0\"" in converter
    assert "os.link(image_path, destination)" in converter
    assert '"person",\n    "person",' in trainer
    assert '"car",\n    "car",' in trainer
