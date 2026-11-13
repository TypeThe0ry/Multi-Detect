from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from multidetect.simulator import SimulatorOrchestrator, SimulatorProcessSpec
from multidetect.simulator.orchestrator import _safe_process_pid, _safe_process_poll

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "configs/scenarios/river_rescue_vtol_001.scenario.json"


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as channel:
        channel.bind(("127.0.0.1", 0))
        return int(channel.getsockname()[1])


@pytest.mark.usefixtures("require_loopback_sockets")
def test_orchestrator_applies_signed_throttle_mode_with_default_endpoint(
    tmp_path: Path,
) -> None:
    orchestrator = SimulatorOrchestrator(
        SCENARIO,
        dry_run=True,
        job_root=tmp_path,
        qgc_control_port=_free_udp_port(),
        throttle_wire_mode="signed",
    )

    result = orchestrator.start()

    assert result.control["throttle_wire_mode"] == "signed"
    assert result.control["endpoint"] == "udpout:127.0.0.1:14540"
    job = json.loads((result.job_dir / "job.json").read_text(encoding="utf-8"))
    assert job["throttle_wire_mode"] == "signed"


def test_process_evidence_helpers_tolerate_minimal_or_broken_doubles() -> None:
    class Minimal:
        pid = "1234"
        returncode = "-7"

    class PollOnly:
        pid = 4321

        def poll(self) -> int:
            return 3

    class Broken:
        @property
        def pid(self) -> object:
            raise RuntimeError("pid unavailable")

        def poll(self) -> object:
            raise RuntimeError("poll unavailable")

    assert _safe_process_pid(Minimal()) == 1234
    assert _safe_process_poll(Minimal()) == -7
    assert _safe_process_pid(PollOnly()) == 4321
    assert _safe_process_poll(PollOnly()) == 3
    assert _safe_process_pid(Broken()) is None
    assert _safe_process_poll(Broken()) is None
    assert _safe_process_pid(None) is None
    assert _safe_process_poll(None) is None


@pytest.mark.usefixtures("require_loopback_sockets")
def test_orchestrator_serializes_process_without_pid_or_poll(tmp_path: Path) -> None:
    class MinimalProcess:
        returncode: int | None = None

        def terminate(self) -> None:
            self.returncode = 0

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            self.returncode = 0
            return 0

    process = MinimalProcess()
    orchestrator = SimulatorOrchestrator(
        SCENARIO,
        dry_run=False,
        job_root=tmp_path,
        qgc_control_port=_free_udp_port(),
        px4_control_endpoint=f"udpout:127.0.0.1:{_free_udp_port()}",
        process_factory=lambda *_args, **_kwargs: process,
        process_specs=(SimulatorProcessSpec("minimal", ("fake",)),),
    )

    result = orchestrator.start()
    assert result.status == "running"
    assert result.processes[0]["pid"] is None
    assert result.processes[0]["returncode"] is None

    orchestrator.stop()
    assert orchestrator.last_result is not None
    stopped = orchestrator.last_result
    assert stopped.status == "stopped"
    # Handles are released internally, but the public stopped result keeps
    # the final process metadata for post-run diagnostics and provenance.
    assert len(stopped.processes) == 1
    assert stopped.processes[0]["name"] == "minimal"
    assert stopped.processes[0]["pid"] is None
    assert stopped.processes[0]["returncode"] == 0
    result_document = json.loads((stopped.job_dir / "result.json").read_text(encoding="utf-8"))
    assert result_document["processes"] == list(stopped.processes)
    assert process.returncode == 0


@pytest.mark.usefixtures("require_loopback_sockets")
def test_orchestrator_closes_parent_log_handle_after_process_start(tmp_path: Path) -> None:
    class MinimalProcess:
        returncode: int | None = None

        def terminate(self) -> None:
            self.returncode = 0

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            self.returncode = 0
            return 0

    opened: list[object] = []

    def factory(*_args: object, **kwargs: object) -> MinimalProcess:
        opened.append(kwargs["stdout"])
        return MinimalProcess()

    orchestrator = SimulatorOrchestrator(
        SCENARIO,
        dry_run=False,
        job_root=tmp_path,
        qgc_control_port=_free_udp_port(),
        px4_control_endpoint=f"udpout:127.0.0.1:{_free_udp_port()}",
        process_factory=factory,
        process_specs=(SimulatorProcessSpec("minimal", ("fake",)),),
    )

    result = orchestrator.start()
    assert result.status == "running"
    assert len(opened) == 1
    assert opened[0].closed is True
    orchestrator.stop()
