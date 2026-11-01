import json
from pathlib import Path

from scripts.audit_r21id_r4_collection import audit_collection


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _make_episode(root: Path, *, status: str = "complete", failure: str | None = None) -> Path:
    episode = root / "episode-0000-test"
    (episode / "rgb").mkdir(parents=True)
    metadata = {
        "episode_id": "episode-0000-test",
        "status": status,
        "task_text": "找到皮卡并记录位置",
        "provenance": {"command": ["collect", "--scenario", "gen_r21id_r4_w0000.scenario.json"]},
    }
    if failure:
        metadata["failure"] = failure
    _write_json(episode / "episode.json", metadata)
    if status == "complete":
        rows = []
        views = ("front", "oblique", "edge_truncated", "recovery")
        for view_index, view in enumerate(views):
            for frame_index in range(8):
                name = f"{view_index:01d}-{frame_index:02d}.jpg"
                (episode / "rgb" / name).write_bytes(b"frame")
                rows.append({
                    "labels": {
                        "phase": "observe",
                        "target_in_fov": True,
                        "observation_view_index": view_index,
                        "observation_view": view,
                    },
                    "obs_before": {
                        "pose": {"alt_m": 18.0, "local_ned_m": [0.0, 0.0, -18.0]},
                        "rgb_path": f"rgb/{name}",
                        "camera_info": {"fy": 540.0, "cy": 480.0},
                    },
                })
        (episode / "steps.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows),
            encoding="utf-8",
        )
    return episode


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    episodes = tmp_path / "episodes"
    complete = _make_episode(episodes / "gen_r21id_r4_w0000" / "ep-0" / "attempt-0" / "episodes")
    _make_episode(
        episodes / "gen_r21id_r4_w0000" / "ep-0" / "attempt-1" / "episodes",
        status="failed",
        failure="TimeoutError: PX4 did not confirm armed heartbeat after attempts",
    )
    _make_episode(
        episodes / "gen_r21id_r4_w0000" / "ep-1" / "attempt-0" / "episodes",
        status="failed",
        failure="TimeoutError: episode wall-clock timeout",
    )
    round_state = tmp_path / "round-state.json"
    _write_json(round_state, {
        "episodes": {
            "gen_r21id_r4_w0000:0": {"status": "complete", "path": str(complete / "episode.json")},
            "gen_r21id_r4_w0000:1": {"status": "failed", "attempts_total": 1},
        }
    })
    scenarios = tmp_path / "scenarios"
    _write_json(scenarios / "gen_r21id_r4_w0000.teacher_intent.json", {
        "observation": {
            "camera_altitude_m": 18.0,
            "observation_waypoint": {"x_east_m": 0.0, "y_north_m": 0.0},
            "views": [
                {"view": view, "observation_waypoint": {"x_east_m": 0.0, "y_north_m": 0.0}}
                for view in ("front", "oblique", "edge_truncated", "recovery")
            ],
        }
    })
    _write_json(scenarios / "gen_r21id_r4_w0000.truth.json", {
        "true_target_id": "target",
        "targets": [{"target_id": "target", "true_target": True, "pose": {"x_east_m": 10.0, "y_north_m": 0.0}}],
    })
    index = tmp_path / "index.json"
    _write_json(index, {"worlds": [{
        "world_id": "gen_r21id_r4_w0000",
        "target_combo": {"category": "pickup"},
        "paired_confusers": ["suv"],
        "view_geometry": {
            "ground_distance_m": 10.0,
            "target_short_side_px": "8-12",
            "target_short_side_px_estimate": 10.0,
        },
    }]})
    return episodes, round_state, index, scenarios


def test_audit_requires_all_four_views_and_groups_failures(tmp_path):
    episodes, round_state, index, scenarios = _fixture(tmp_path)
    report = audit_collection(
        episodes_root=episodes,
        round_state=round_state,
        index_path=index,
        scenarios=scenarios,
    )
    assert report["logical_complete_count"] == 1
    assert report["qualified_episode_count"] == 1
    assert report["qualified_worlds"] == ["gen_r21id_r4_w0000"]
    assert report["logical_failed_count"] == 1
    assert report["failed_artifact_count"] == 2
    assert report["failure_groups"] == {
        "PX4 未确认 armed heartbeat": 1,
        "episode wall-clock timeout": 1,
    }


def test_audit_marks_missing_view(tmp_path):
    episodes, round_state, index, scenarios = _fixture(tmp_path)
    complete = next((episodes / "gen_r21id_r4_w0000" / "ep-0" / "attempt-0" / "episodes").glob("episode-*/steps.jsonl"))
    rows = [json.loads(line) for line in complete.read_text().splitlines()]
    rows = [row for row in rows if row["labels"]["observation_view"] != "recovery"]
    complete.write_text("".join(json.dumps(row) + "\n" for row in rows))
    report = audit_collection(
        episodes_root=episodes,
        round_state=round_state,
        index_path=index,
        scenarios=scenarios,
    )
    assert report["qualified_episode_count"] == 0
    assert any("缺视角: recovery" in issue for issue in report["complete_reports"][0]["issues"])
