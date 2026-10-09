"""CPU wiring fixtures do not certify native GPU or external Gazebo flight."""

from __future__ import annotations

import json

import pytest
from test_onboard_live_shadow import FixtureWorker, source

from scripts import run_onboard_gazebo_live_shadow as cli

REVISION = "89644892e4d85e24eaac8bacfd4f463576704203"


def args(tmp_path, *extra):
    snapshot = tmp_path / REVISION
    snapshot.mkdir(exist_ok=True)
    return cli._parser().parse_args(
        [
            "--prompt",
            "  检查前方是否有可安全观察的目标\n",
            "--snapshot",
            str(snapshot),
            "--model-sha256",
            "a" * 64,
            "--output-directory",
            str(tmp_path / "run"),
            "--sensor-frames",
            "2",
            "--deadline",
            "1",
            *extra,
        ]
    )


class Worker(FixtureWorker):
    def __init__(self, factory, **kwargs):
        super().__init__()
        self.factory = factory
        self.closed = False

    def close(self):
        self.closed = True
        self.state = "closed"


def capture(_scenario, _report, **kwargs):
    for sequence in range(kwargs["sensor_frame_count"]):
        kwargs["sensor_frame_callback"](*source(sequence), None)
    return {"gate_passed": True, "fixture_not_external_sim": True}


def read_report(options):
    return json.loads((options.output_directory / "report.json").read_text(encoding="utf-8"))


def test_preflight_never_starts_worker_or_camera(tmp_path):
    options = args(tmp_path)
    calls = []

    def planned(_scenario, _report, **kwargs):
        calls.append(kwargs)
        assert kwargs["execute"] is False and "sensor_frame_callback" not in kwargs
        return {"gate_passed": True}

    def forbidden(*_a, **_kw):
        raise AssertionError("preflight must not start worker")

    assert cli.run(options, capture_runner=planned, worker_factory=forbidden) == 0
    result = read_report(options)
    assert len(calls) == 1 and result["status"] == "planned_not_executed"
    assert result["shadow"] is None and result["model_actuated_flight"] is False
    assert result["prompt_text_original"] == options.prompt


def test_execute_cpu_fixture_actual_hook_and_cleanup(tmp_path):
    options = args(tmp_path, "--execute")
    workers = []

    def make(*a, **kw):
        workers.append(Worker(*a, **kw))
        return workers[-1]

    assert cli.run(options, capture_runner=capture, worker_factory=make) == 0
    result = read_report(options)
    assert result["status"] == "capture_and_shadow_passed"
    assert result["shadow"]["recorded_frames"] == 2
    assert result["shadow"]["strict_decisions"] >= 1
    assert workers[0].closed and result["cleanup_errors"] == []
    assert result["vlm_prompt_task_completed"] is result["model_actuated_flight"] is False
    assert result["source_records_unchanged"] is True


def test_close_failure_does_not_discard_capture_or_shadow(tmp_path):
    class FailingClose(Worker):
        def close(self):
            raise RuntimeError("fixture teardown failure")

    options = args(tmp_path, "--execute")
    assert cli.run(options, capture_runner=capture, worker_factory=FailingClose) == 1
    result = read_report(options)
    assert result["capture"]["gate_passed"] is True
    assert result["shadow"]["strict_decisions"] >= 1
    assert "worker.close" in result["cleanup_errors"][0]
    assert result["status"] == "failed_partial" and result["native_exit_code"] == 1


def test_bridge_finish_failure_still_closes_worker_and_saves_report(tmp_path, monkeypatch):
    original_bridge = cli.OnboardLiveShadowBridge

    class FailingFinish(original_bridge):
        def finish(self, **kwargs):
            super().finish(**kwargs)
            raise RuntimeError("fixture finish failure")

    monkeypatch.setattr(cli, "OnboardLiveShadowBridge", FailingFinish)
    workers = []

    def make(*a, **kw):
        workers.append(Worker(*a, **kw))
        return workers[-1]

    options = args(tmp_path, "--execute")
    assert cli.run(options, capture_runner=capture, worker_factory=make) == 1
    result = read_report(options)
    assert workers[0].closed and result["capture"]["gate_passed"]
    assert result["shadow"]["closed"]
    assert any("bridge.finish" in item for item in result["cleanup_errors"])


def test_capture_failure_keeps_partial_episode(tmp_path):
    def failed(_scenario, _report, **kwargs):
        kwargs["sensor_frame_callback"](*source(0), None)
        raise RuntimeError("fixture capture stopped")

    options = args(tmp_path, "--execute")
    assert cli.run(options, capture_runner=failed, worker_factory=Worker) == 1
    result = read_report(options)
    assert result["shadow"]["recorded_frames"] == 1
    assert "capture stopped" in result["error"]
    assert result["status"] == "failed_partial"


def test_prewarm_failure_does_not_start_capture(tmp_path):
    class FailedStart(Worker):
        def start(self):
            self.state, self.error = "failed", "fixture load failure"

    def forbidden(*_a, **_kw):
        raise AssertionError("capture started before prewarm")

    options = args(tmp_path, "--execute")
    assert cli.run(options, capture_runner=forbidden, worker_factory=FailedStart) == 1
    result = read_report(options)
    assert result["capture"] is None and "prewarm failed" in result["error"]


@pytest.mark.parametrize(
    "extra",
    [
        ["--baseline-flight"],
        ["--control-port", "14550"],
        ["--telemetry-port", "14550"],
        ["--telemetry-port", "24570"],
        ["--control-port", "0"],
        ["--telemetry-port", "65536"],
        ["--history-frames", "5"],
        ["--deadline", "nan"],
        ["--baseline-flight", "--execute", "--sensor-frames", "10000"],
    ],
)
def test_invalid_requests_do_not_create_run_or_start_tools(tmp_path, extra):
    options = args(tmp_path, *extra)
    with pytest.raises(ValueError):
        cli.run(options)
    assert not options.output_directory.exists()


def test_existing_run_is_not_overwritten(tmp_path):
    options = args(tmp_path)
    options.output_directory.mkdir()
    with pytest.raises(FileExistsError):
        cli.run(options)


def test_source_reopen_error_is_evidenced_and_report_still_saved(tmp_path, monkeypatch):
    options = args(tmp_path)
    original = cli._record
    calls = 0

    def flaky(path):
        nonlocal calls
        calls += 1
        if calls > 9:
            raise FileNotFoundError("fixture source vanished")
        return original(path)

    monkeypatch.setattr(cli, "_record", flaky)
    assert cli.run(options, capture_runner=lambda *_a, **_kw: {"gate_passed": True}) == 1
    result = read_report(options)
    assert result["source_records_unchanged"] is False
    assert any("error" in row for row in result["source_recheck"])
    assert result["status"] == "failed_partial"
