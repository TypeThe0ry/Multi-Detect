from __future__ import annotations

import json
import gzip
from pathlib import Path

from scripts import collect_sim_teacher_episodes as collector
from scripts.analyze_sensor_timeouts import analyze_episode, main


def test_host_pressure_sampler_writes_proc_and_core_busy_fields(
    tmp_path: Path, monkeypatch
) -> None:
    sampler = collector.HostPressureSampler(tmp_path, (2, 4), interval_s=0.01)
    monkeypatch.setattr(collector, "_proc_cpu_ticks", lambda: {2: (100, 20), 4: (100, 60)})
    monkeypatch.setattr(
        sampler,
        "_sample",
        lambda previous: (
            {
                "wall_time": 1.0,
                "monotonic_ns": 2,
                "loadavg": "1.00 2.00 3.00 1/10 12",
                "cpu_pressure": {"avg10": 0.1},
                "memory_pressure": None,
                "io_pressure": None,
                "cpuset_cpus": [2, 4],
                "core_busy_percent_mean": 50.0,
                "core_busy_percent_max": 80.0,
                "px4_sim_time_boot_ms": 1234,
            },
            {2: (100, 20), 4: (100, 60)},
        ),
    )
    sampler.start()
    sampler.stop_event.wait(0.03)
    sampler.close()
    rows = [
        json.loads(line)
        for line in (tmp_path / "host_pressure.jsonl").read_text().splitlines()
    ]
    assert rows
    assert rows[0]["cpuset_cpus"] == [2, 4]
    assert rows[0]["core_busy_percent_mean"] == 50.0
    assert rows[0]["cpu_pressure"]["avg10"] == 0.1
    assert rows[0]["px4_sim_time_boot_ms"] == 1234


def test_sampling_failure_does_not_escape_background_thread(tmp_path: Path, monkeypatch) -> None:
    sampler = collector.HostPressureSampler(tmp_path, (0,), interval_s=0.01)

    def fail(_previous):
        raise OSError("proc unavailable")

    monkeypatch.setattr(sampler, "_sample", fail)
    sampler.start()
    sampler.stop_event.wait(0.02)
    sampler.close()
    assert not (tmp_path / "host_pressure.jsonl").exists()


def test_sensor_timeout_report_includes_rtf_and_pressure(tmp_path: Path, capsys) -> None:
    episode = tmp_path / "episode-1"
    episode.mkdir()
    (episode / "episode.json").write_text(
        json.dumps(
            {
                "status": "complete",
                "created_monotonic_ns": 1_000_000_000,
                "finished_monotonic_ns": 11_000_000_000,
            }
        ),
        encoding="utf-8",
    )
    (episode / "mavlink.jsonl").write_text(
        json.dumps({"message_type": "STATUSTEXT", "text": "Accel #0 fail: TIMEOUT!"}) + "\n"
        + json.dumps({"message_type": "STATUSTEXT", "text": "MAG #0 failed: TIMEOUT!"}) + "\n",
        encoding="utf-8",
    )
    (episode / "steps.jsonl").write_text(
        json.dumps(
            {
                "obs_before": {"pose_time_boot_ms": 1000},
                "obs_after": {"pose_time_boot_ms": 11000},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (episode / "host_pressure.jsonl").write_text(
        json.dumps(
            {
                "loadavg": "4.0 3.0 2.0",
                "core_busy_percent_mean": 40,
                "core_busy_percent_max": 70,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    report = analyze_episode(episode)
    assert report["timeouts"] == {"accel": 1, "gyro": 0, "mag": 1}
    assert report["rtf"] == 1.0
    assert report["host_pressure"]["core_busy_percent_max_peak"] == 70.0
    assert main([str(episode)]) == 0
    assert "episode-1\tcomplete\t1\t0\t1\t1.0" in capsys.readouterr().out


def test_sensor_timeout_report_reads_gzip_and_matches_trace(tmp_path: Path) -> None:
    episode = tmp_path / "episode-1"
    episode.mkdir()
    (episode / "episode.json").write_text(
        json.dumps(
            {
                "status": "complete",
                "created_wall_time": 100.0,
                "finished_wall_time": 130.0,
                "created_monotonic_ns": 1,
                "finished_monotonic_ns": 11_000_000_001,
            }
        ),
        encoding="utf-8",
    )
    with gzip.open(episode / "mavlink.jsonl.gz", "wt", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "message_type": "STATUSTEXT",
                    "text": "Gyro #0 fail: TIMEOUT!",
                    "receive_wall_time": 110.0,
                }
            )
            + "\n"
        )
    report = analyze_episode(episode)
    assert report["timeouts"] == {"accel": 0, "gyro": 1, "mag": 0}
    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        json.dumps(
            {
                "t": 100.0,
                "load": 2.0,
                "cpu": {"some": 10.0},
                "top_cores": {"quiet.service": 1.0},
            }
        )
        + "\n"
        + json.dumps(
            {
                "t": 110.5,
                "load": 8.0,
                "cpu": {"some": 80.0},
                "top_cores": {"burst.service": 9.0},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    from scripts.analyze_sensor_timeouts import _trace_rows, correlate_trace

    correlation = correlate_trace([report], _trace_rows(trace))
    assert correlation["events"][0]["trace_sample"]["cpu_psi_some"] == 80.0
    assert correlation["episodes"][0]["samples"] == 2
    assert correlation["burst_vs_quiet"] == {
        "burst_timeout_episodes": 1,
        "burst_no_timeout_episodes": 0,
        "quiet_timeout_episodes": 0,
        "quiet_no_timeout_episodes": 0,
        "fisher_two_sided_p": 1.0,
    }


def test_sensor_timeout_main_accepts_episode_root(tmp_path: Path, capsys) -> None:
    episode = tmp_path / "nested" / "episode-1"
    episode.mkdir(parents=True)
    (episode / "episode.json").write_text("{}", encoding="utf-8")
    assert main([str(tmp_path)]) == 0
    assert "episode-1" in capsys.readouterr().out


def test_sensor_timeout_reports_missing_sidecar_and_frame_wait_timeline(tmp_path: Path) -> None:
    episode = tmp_path / "episode-missing-pressure"
    episode.mkdir()
    (episode / "episode.json").write_text(
        json.dumps(
            {
                "status": "failed",
                "created_wall_time": 10.0,
                "finished_wall_time": 26.0,
                "frame_wait_timeline": [
                    {"phase": "obs_after", "failure_class": "frame_timestamp_below_pose_watermark", "error": "TimeoutError"}
                ],
            }
        ),
        encoding="utf-8",
    )
    report = analyze_episode(episode)
    assert report["host_pressure_sidecar"]["present"] is False
    assert report["host_pressure_sidecar"]["sample_count"] == 0
    assert report["frame_wait_failure_count"] == 1
    assert report["frame_wait_failure_classes"] == ["frame_timestamp_below_pose_watermark"]
