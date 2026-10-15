import pytest

from multidetect.autonomy.onboard_loop import CameraModel, Detection, OnboardLoop
from scripts.evaluate_small_object_tiling import filter_boxes, summarize


def test_benchmark_filter_matches_onboard_size_area_and_top_eight():
    boxes = [[0, 0, 100, 100, 1.0], [0, 0, 5, 10, 0.99]]
    boxes += [[i, i, i + 6, i + 8, i / 20] for i in range(12)]
    loop = OnboardLoop(
        camera=CameraModel(fx=1, fy=1, cx=50, cy=50, width=100, height=100),
        search_action=lambda *_: None,
        segment_clear=lambda *_: True,
    )
    expected = loop.filter_detections([Detection(tuple(b[:4]), b[4]) for b in boxes])
    assert filter_boxes(boxes, 100, 100) == [[*d.bbox, d.score] for d in expected]
    assert len(expected) == 8


def test_summary_distinguishes_raw_default_filter_and_optional_minimum():
    tiny, larger = [0, 0, 4, 4, 0.9], [20, 20, 28, 28, 0.8]
    objects = [
        {"category": "traffic_cone", "box": tiny[:4], "size_bucket": "<8"},
        {"category": "backpack", "box": larger[:4], "size_bucket": "8-16"},
    ]
    frame = {
        "image": "frame", "image_key": "frame", "objects": objects, "scene": objects,
        "altitude_bucket": "16-20", "cached_boxes": [], "width": 100, "height": 100,
    }
    row = {
        "image": "frame", "single": [tiny], "tiles": [tiny, larger],
        "hybrid": [tiny, larger], "single_seconds": 1, "tiles_seconds": 4,
        "hybrid_seconds": 5,
    }
    result = summarize([frame], [row])

    def recall(mode):
        return result[mode]["tables"]["small_overall"][0]["recall_iou_0.3"]

    assert recall("single_raw") == 0.5
    assert recall("tiles_raw") == 1
    assert recall("hybrid_onboard") == 0.5
    assert recall("hybrid_onboard_min2") == 1
    assert result["timing_seconds"]["hybrid"]["mean"] == 5
    assert result["cached_onboard"]["proposals"] == 0


@pytest.mark.parametrize(
    "frames,rows",
    [([], []), ([{"image": "one"}], []), ([{"image": "one"}], [{"image": "two"}])],
)
def test_summary_rejects_empty_incomplete_or_misaligned_results(frames, rows):
    with pytest.raises(ValueError):
        summarize(frames, rows)
