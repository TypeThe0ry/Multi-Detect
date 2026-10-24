from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from scripts.render_episode_report import render_episode_report


@pytest.fixture
def episode_fixture(tmp_path: Path):
    episode = tmp_path / "gen_fixture" / "episodes" / "episode-0000-test"
    episode.mkdir(parents=True)
    metadata = {
        "status": "complete",
        "task_text": "count hydrants",
        "provenance": {"git_commit": "abc123"},
        "onboard_config": {"refs": {"zone:work": [0, 0, 30]}},
    }
    (episode / "episode.json").write_text(json.dumps(metadata), encoding="utf-8")
    rows = [
        {
            "step_index": 0,
            "task_text": "count hydrants",
            "sim_time_s": 1.0,
            "obs_before": {"pose": {"local_ned_m": [0, 0, -10]}},
            "obs_after": {"pose": {"local_ned_m": [1, 0, -11]}},
            "action": {"kind": "translate"},
            "labels": {
                "onboard": {
                    "tracks": [{"id": "h1", "ground_ne": [10.5, 0], "bbox": [1, 2, 21, 32]}],
                    "slow_calls": [{"track_id": "h1", "decision": "count", "confidence": 0.9}],
                    "mission": {"counts": {"s1": 1}, "positions": ["10.5,0"]},
                    "safety": {"rejected": False, "reasons": []},
                }
            },
        },
        {
            "step_index": 1,
            "task_text": "count hydrants",
            "sim_time_s": 2.0,
            "obs_before": {"pose": {"local_ned_m": [1, 0, -11]}},
            "obs_after": {"pose": {"local_ned_m": [2, 0, -12]}},
            "action": {"kind": "turn"},
            "labels": {
                "onboard": {
                    "tracks": [],
                    "slow_calls": [],
                    "mission": {"counts": {"s1": 1}, "positions": ["10.5,0"]},
                    "revisit_candidate_snapshots": [
                        {"selected": False, "reason": "budget_exhausted"}
                    ],
                    "safety": {"rejected": True, "reasons": ["geofence"]},
                }
            },
        },
    ]
    (episode / "steps.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows), encoding="utf-8"
    )
    truth = {
        "targets": [
            {
                "target_id": "hit",
                "true_target": True,
                "count_member": True,
                "category_words": ["hydrant"],
                "pose": {"y_north_m": 10, "x_east_m": 0},
            },
            {
                "target_id": "miss",
                "count_member": True,
                "category_words": ["hydrant"],
                "pose": {"y_north_m": 25, "x_east_m": 0},
            },
            {"target_id": "obstacle", "class": "building", "pose": {"y_north_m": 8, "x_east_m": 9}},
        ]
    }
    return episode, truth


def test_report_contains_map_timeline_height_provenance_and_count_comparison(
    tmp_path, episode_fixture
):
    episode, truth = episode_fixture
    truth_path = tmp_path / "truth.json"
    truth_path.write_text(json.dumps(truth), encoding="utf-8")
    output = tmp_path / "report.html"
    result = render_episode_report(episode, output, truth_path=truth_path, thumbnails=False)
    page = output.read_text(encoding="utf-8")
    assert result["steps"] == 2 and result["truth_targets"] == 2
    assert 'id="map"' in page and 'class="work-area"' in page
    assert "高度-时间" in page and "决策时间线" in page
    assert "配置 SHA-256" in page and "abc123" in page
    assert "budget_exhausted" in page and "geofence" in page
    assert "已计入" in page and "漏计" in page
    assert not re.search(r"https?://|(?:src|href)=[\"'](?:https?:|//)", page)
    assert "data:image" not in page


def test_truth_unavailable_and_batch_discovery(tmp_path, episode_fixture):
    episode, _ = episode_fixture
    output = tmp_path / "unknown.html"
    render_episode_report(episode, output, thumbnails=False)
    assert "真值不可用" in output.read_text(encoding="utf-8")
