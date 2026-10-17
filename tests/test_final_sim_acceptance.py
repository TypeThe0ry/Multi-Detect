import json
from pathlib import Path

from scripts.run_final_sim_acceptance import _result_metrics, render_report


def test_result_metrics_groups_position_sources_and_summary():
    report = {
        "summary": {"success_rate": 0.5, "false_confirm_rate": 0.25, "count_exact_rate": 1.0},
        "episodes": [
            {"position_source": "ground", "position_error_m": 2.0},
            {"position_source": "triangulation", "position_error_m": 1.0},
            {"position_source": "fused", "position_error_m": 0.5},
        ],
    }
    metrics = _result_metrics(report)
    assert metrics["success_rate"] == 0.5
    assert metrics["position_error_by_source_m"] == {"ground": 2.0, "triangulation": 1.0, "fused": 0.5}


def test_render_report_contains_history_and_decision_rule(tmp_path: Path):
    output = tmp_path / "final-report.md"
    render_report([{"stage": "测试世界", "variant": "triangulation", "units": 12, "metrics": {}}], output, dry_run=True)
    text = output.read_text(encoding="utf-8")
    assert "6-8/12" in text
    assert "三角化默认打开须同时满足" in text
    assert "仿真近似" in text
