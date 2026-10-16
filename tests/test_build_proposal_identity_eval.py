from __future__ import annotations

from scripts.build_proposal_identity_eval import (
    _normalised_candidates,
    associate,
    iou,
    proposal_coverage,
    size_bucket,
)


def test_half_open_iou_and_coverage_boundaries() -> None:
    assert iou((0, 0, 10, 10), (10, 0, 20, 10)) == 0.0
    assert iou((0, 0, 10, 10), (0, 0, 10, 10)) == 1.0
    assert proposal_coverage((0, 0, 10, 10), (2, 2, 8, 8)) == 1.0


def test_association_primary_ambiguous_and_background() -> None:
    obj = {"target_id": "t", "box": (0, 0, 10, 10)}
    assert associate((0, 0, 10, 10), [obj])[1] == "associated"
    assert associate((9, 0, 19, 10), [obj])[1] == "ambiguous"
    assert associate((20, 20, 30, 30), [obj])[1] == "background"
    assert associate((0, 0, 1, 1), [])[1] == "background"


def test_size_bucket_edges() -> None:
    assert [size_bucket(x) for x in (11.99, 12, 15.99, 16, 19.99, 20, 31.99, 32)] == [
        "<12", "12-16", "12-16", "16-20", "16-20", "20-32", "20-32", ">=32"
    ]


def test_normalised_candidates_clamps_and_drops_invalid() -> None:
    rows = _normalised_candidates([[-2, 0, 12, 20, 0.8], [10, 10, 5, 5, 0.9], [float("nan"), 0, 1, 1]], 10, 10)
    assert len(rows) == 1
    assert rows[0]["box_xyxy_normalized"] == [0.0, 0.0, 1.0, 1.0]
    assert rows[0]["size_bucket"] == "<12"
