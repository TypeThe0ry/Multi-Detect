import pytest

from scripts.run_yoloe_v3_round_eval import summarize_recall, summarize_timing


def test_summarize_recall_reports_overall_sizes_and_unseen_classes():
    def result(overall, small, unseen):
        rows = [
            {
                "group": "all", "objects": 10,
                "recall_iou_0.3": overall, "recall_iou_0.5": overall / 2,
            },
            {"group": "<8", "objects": 2, "recall_iou_0.3": small, "recall_iou_0.5": small / 2},
            {
                "group": "stop_sign", "objects": 1,
                "recall_iou_0.3": unseen, "recall_iou_0.5": unseen / 2,
            },
        ]
        return {
            "one_to_one_recall": {
                "tables": {"overall": rows[:1], "size": rows[1:2], "category": rows[2:]}
            }
        }

    report = {"metrics": {"1280": {"old": result(0.4, 0.1, 0.0), "new": result(0.6, 0.3, 1.0)}}}
    summary = summarize_recall(report)["by_imgsz"]["1280"]
    assert summary["overall"]["all"]["iou_0.3"]["delta"] == pytest.approx(0.2)
    assert summary["size"]["<8"]["iou_0.3"]["delta"] == pytest.approx(0.2)
    assert summary["unseen_classes"]["stop_sign"]["iou_0.3"]["delta"] == 1.0
    assert summary["unseen_classes"]["postbox"] is None


def test_summarize_timing_reports_old_new_delta_for_each_mode():
    modes = ("single", "tiles", "hybrid")
    old = {
        "timing_seconds": {
            mode: {"mean": 1.0, "median": 0.8, "p95": 2.0} for mode in modes
        }
    }
    new = {
        "timing_seconds": {
            mode: {"mean": 1.5, "median": 1.1, "p95": 2.4} for mode in modes
        }
    }
    result = summarize_timing(old, new)
    assert result["hybrid"]["mean"] == {"old": 1.0, "new": 1.5, "delta": 0.5}
    assert result["single"]["p95"]["delta"] == pytest.approx(0.4)
