import json
from pathlib import Path

from scripts.analyze_px4_clock_stall_timeline import analyze_episode, main

REAL_EPISODE = Path(
    "/data/projects/Multi-Detect-env/runs/src-followup2/.tmp/jobs/climb-fault-sitl4-1169782/"
    "artifacts/onboard-climb-fault-sitl4-20261108T002732Z-1169782/operator-abort/"
    "campaign/gen_cnt2_w0003/episodes/episode-0000-540aee44"
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_real_episode_reports_climb_rtf_and_cross_clock_boundary():
    report = analyze_episode(REAL_EPISODE)
    climb = report["climb_primitives"]

    assert len(climb) == 1
    assert climb[0]["status"] == "timeout"
    assert climb[0]["px4_elapsed_s"] == 20.196
    assert 0.21 < climb[0]["rtf"] < 0.22
    assert report["mavlink_local_position"]["last"]["px4_s"] == 69.672
    assert report["capture_analysis"]["gz_clock_advanced"] is True
    assert report["capture_analysis"]["imu_advanced"] is False
    assert report["capture_analysis"]["camera_advanced"] == "unknown"
    assert 48.8 < report["capture_samples"][0][
        "seconds_since_last_mavlink_local_position"
    ] < 48.9
    assert 67.9 < report["capture_samples"][1][
        "seconds_since_last_mavlink_local_position"
    ] < 68.1
    assert report["host_pressure"]["sample_count"] == 82
    assert report["clock_alignment"]["capture_clock_conversion_is_derived"] is True


def test_missing_evidence_is_explicit_and_analyzer_is_read_only(tmp_path: Path):
    episode = tmp_path / "episode"
    episode.mkdir()
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    report = analyze_episode(episode)
    after = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    assert before == after == [Path("episode")]
    assert set(report["missing_files"]) == {
        "steps.jsonl",
        "mavlink.jsonl",
        "host_pressure.jsonl",
        "px4-clock-stall-capture.json",
    }
    assert report["mavlink_local_position"]["last"] is None


def test_synthetic_capture_uses_wall_monotonic_anchor_and_preserves_unknowns(tmp_path: Path):
    episode = tmp_path / "episode"
    episode.mkdir()
    (episode / "episode.json").write_text(
        json.dumps({"created_wall_time": 1000.0, "created_monotonic_ns": 900_000_000_000}),
        encoding="utf-8",
    )
    _write_jsonl(
        episode / "steps.jsonl",
        [
            {
                "step_index": 0,
                "action": {"kind": "translate", "up_m": 5},
                "outcome": {"status": "timeout"},
                "obs_before": {"pose_time_boot_ms": 1000},
                "obs_after": {"pose_time_boot_ms": 3000},
                "timing": {"command_and_primitive_wait_ms": 10000},
            }
        ],
    )
    (episode / "px4-clock-stall-capture.json").write_text(
        json.dumps(
            {
                "reason": "wall_timeout_s=45",
                "px4_clock_diagnostics": {
                    "last_progress_at_monotonic_s": 950.0,
                    "last_progress_px4_clock_s": 3.0,
                },
                "samples": [
                    {
                        "label": "deadline",
                        "captured_wall_time": 1005.0,
                        "gz_clock": {"latest_sim_time_s": 8.0},
                    },
                    {
                        "label": "after_3s",
                        "captured_wall_time": 1008.0,
                        "gz_clock": {"latest_sim_time_s": 8.2},
                    },
                ],
                "analysis": {
                    "gz_clock_advanced": True,
                    "sensor_progress": {
                        "imu": {"advanced": False},
                        "vehicle_status": {"advanced": False},
                    },
                    "camera_progress": {"advanced": "unknown"},
                },
            }
        ),
        encoding="utf-8",
    )
    report = analyze_episode(episode)

    assert report["climb_primitives"][0]["rtf"] == 0.2
    assert report["capture_samples"][0]["monotonic_s"] == 905.0
    assert (
        report["capture_samples"][0]["monotonic_source"]
        == "derived_from_episode_wall_monotonic_anchor"
    )
    assert report["capture_samples"][0]["watchdog_deadline_monotonic_s"] == 995.0
    assert report["capture_samples"][0]["px4_s"] == 3.0
    assert report["capture_analysis"]["camera_advanced"] == "unknown"


def test_clock_progress_stays_distinct_from_high_host_pressure(tmp_path: Path):
    episode = tmp_path / "episode"
    episode.mkdir()
    (episode / "episode.json").write_text(
        json.dumps(
            {
                "created_wall_time": 1000.0,
                "created_monotonic_ns": 900_000_000_000,
                "finished_wall_time": 1003.0,
                "frame_wait_timeline": [
                    {
                        "phase": "obs_before",
                        "duration_s": 0.4,
                        "frames_received": 2,
                        "host_pressure_sample": {"private_to_frame_event": True},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    _write_jsonl(
        episode / "mavlink.jsonl",
        [
            {
                "message_type": "LOCAL_POSITION_NED",
                "time_boot_ms": 1000,
                "receive_monotonic_ns": 900_000_000_000,
                "receive_wall_time": 1000.0,
            },
            {
                "message_type": "LOCAL_POSITION_NED",
                "time_boot_ms": 2000,
                "receive_monotonic_ns": 902_000_000_000,
                "receive_wall_time": 1002.0,
            },
        ],
    )
    _write_jsonl(
        episode / "steps.jsonl",
        [{"step_index": 0, "outcome": {"status": "success"}}],
    )
    _write_jsonl(
        episode / "host_pressure.jsonl",
        [
            {
                "wall_time": 1000.0,
                "monotonic_ns": 900_000_000_000,
                "loadavg": "220.0 210.0 200.0",
                "cpu_pressure": {"some": {"avg10": 55.0}},
            },
            {
                "wall_time": 1002.0,
                "monotonic_ns": 902_000_000_000,
                "loadavg": "230.0 220.0 210.0",
                "cpu_pressure": {"some": {"avg10": 60.0}},
            },
        ],
    )
    (episode / "px4-clock-stall-capture.json").write_text(
        json.dumps(
            {
                "reason": "wall_timeout_s=45",
                "px4_clock_diagnostics": {
                    "last_progress_at_monotonic_s": 902.0,
                    "last_progress_px4_clock_s": 2.0,
                },
                "samples": [
                    {
                        "label": "deadline",
                        "captured_wall_time": 1047.0,
                        "gz_clock": {"latest_sim_time_s": 50.0},
                    }
                ],
                "analysis": {},
            }
        ),
        encoding="utf-8",
    )

    report = analyze_episode(episode)

    assert report["step_outcomes"] == {"success": 1}
    assert report["mavlink_local_position"]["progress"]["rtf"] == 0.5
    assert report["mavlink_local_position"]["progress"]["receive_gap_s"]["max"] == 2.0
    assert report["host_pressure"]["cpu_psi_some_avg10"]["max"] == 60.0
    assert report["mavlink_local_position"]["progress"]["capture_watchdog_no_progress_s"] == 45.0
    assert (
        report["capture_samples"][0]["seconds_since_last_mavlink_local_position"] == 45.0
    )
    assert [row["cpu_psi_some_avg10"] for row in report["host_pressure_timeline"]] == [
        55.0,
        60.0,
    ]
    assert report["frame_wait_timeline"] == [
        {
            "phase": "obs_before",
            "duration_s": 0.4,
            "frames_received": 2,
        }
    ]


def test_cli_json_output(tmp_path: Path, capsys):
    (tmp_path / "episode").mkdir()
    assert main([str(tmp_path / "episode"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["read_only"] is True
