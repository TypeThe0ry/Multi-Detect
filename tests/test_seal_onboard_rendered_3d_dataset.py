from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from seal_onboard_rendered_3d_dataset import seal_dataset  # noqa: E402
from train_onboard_model import _verify_rendered_review_receipt  # noqa: E402


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    rows = []
    reviews = []
    for index in range(2):
        rgb_hash = _sha(f"rgb-{index}")
        depth_hash = _sha(f"depth-{index}")
        rows.append(
            {
                "features": [0.0] * 40,
                "task_label": 0,
                "flight_label": 0,
                "regression": [0.0, 0.0, 0.0, 0.0],
                "family": "rendered_3d_test",
                "source": {
                    "frame_id": f"frame-{index}",
                    "rgb_payload_sha256": rgb_hash,
                    "depth_payload_sha256": depth_hash,
                    "truth_injected": False,
                },
            }
        )
        reviews.append(
            {
                "row_index": index,
                "frame_id": f"frame-{index}",
                "rgb_payload_sha256": rgb_hash,
                "depth_payload_sha256": depth_hash,
                "task_label": 0,
                "flight_label": 0,
                "regression": [0.1 * index, 0.0, 0.0, 0.0],
                "decision": "approve",
            }
        )
    dataset = {
        "contract": "multidetect.onboard-rendered-3d-dataset-v1",
        "pixel_level_rendered": True,
        "truth_free": True,
        "truth_injected": False,
        "depth_required": True,
        "external_gazebo_runtime": True,
        "source_manifests": [{"sha256": _sha("scene-manifest")}],
        "rows": rows,
    }
    dataset_path = tmp_path / "rendered.json"
    dataset_path.write_text(json.dumps(dataset, indent=2) + "\n", encoding="utf-8")
    review_path = tmp_path / "review.jsonl"
    review_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in reviews), encoding="utf-8"
    )
    return dataset_path, review_path


def test_seal_requires_explicit_review_confirmation(tmp_path: Path) -> None:
    dataset_path, review_path = _fixture(tmp_path)
    with pytest.raises(ValueError, match="confirm-reviewed"):
        seal_dataset(
            dataset_path,
            review_path,
            tmp_path / "sealed.json",
            annotator="operator-a",
            reviewer="operator-b",
            reviewed_at_utc="2026-10-06T08:00:00Z",
            acknowledge_review=False,
        )


def test_seal_binds_every_reviewed_rgb_depth_row(tmp_path: Path) -> None:
    dataset_path, review_path = _fixture(tmp_path)
    output_path = tmp_path / "sealed.json"
    result = seal_dataset(
        dataset_path,
        review_path,
        output_path,
        annotator="operator-a",
        reviewer="operator-b",
        reviewed_at_utc="2026-10-06T08:00:00Z",
        acknowledge_review=True,
    )
    assert result["training_ready"] is True
    sealed = json.loads(output_path.read_text(encoding="utf-8"))
    assert sealed["labels_from"] == "independent_reviewed_3d_rows"
    assert sealed["rows"][1]["regression"][0] == pytest.approx(0.1)
    receipt_path = output_path.parent / sealed["reviewed_annotation_manifest"]["path"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    _verify_rendered_review_receipt(
        dataset_path=output_path,
        document=sealed,
        receipt_document=receipt,
    )


def test_seal_rejects_same_reviewer_and_annotator(tmp_path: Path) -> None:
    dataset_path, review_path = _fixture(tmp_path)
    with pytest.raises(ValueError, match="different"):
        seal_dataset(
            dataset_path,
            review_path,
            tmp_path / "sealed.json",
            annotator="operator-a",
            reviewer="operator-a",
            reviewed_at_utc="2026-10-06T08:00:00Z",
            acknowledge_review=True,
        )


def test_training_loader_review_seal_rejects_tampered_label(tmp_path: Path) -> None:
    dataset_path, review_path = _fixture(tmp_path)
    output_path = tmp_path / "sealed.json"
    seal_dataset(
        dataset_path,
        review_path,
        output_path,
        annotator="operator-a",
        reviewer="operator-b",
        reviewed_at_utc="2026-10-06T08:00:00Z",
        acknowledge_review=True,
    )
    sealed = json.loads(output_path.read_text(encoding="utf-8"))
    sealed["rows"][0]["task_label"] = 1
    receipt_path = output_path.parent / sealed["reviewed_annotation_manifest"]["path"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="task label mismatch"):
        _verify_rendered_review_receipt(
            dataset_path=output_path,
            document=sealed,
            receipt_document=receipt,
        )
