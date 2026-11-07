from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import collect_sim_teacher_episodes as collector


def _plan(tmp_path: Path, config: dict) -> SimpleNamespace:
    path = tmp_path / "onboard.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return SimpleNamespace(onboard_config=str(path), operator_port=14580)


def test_operator_link_mission_starts_the_gcs_endpoint(tmp_path, monkeypatch) -> None:
    captured = {}

    def fake_start(args, policy, vehicle):
        captured.update(args=args, policy=policy, state=vehicle.snapshot())
        return "server"

    import scripts.onboard_main as onboard_main

    monkeypatch.setattr(onboard_main, "_start_operator_link", fake_start)
    policy = SimpleNamespace()
    config = {
        "mission": "operator_link",
        "operator_port": 14590,
        "operator_hmac_key_hex": "11" * 32,
        "operator_mavlink_signing_key_hex": "22" * 32,
    }
    collector._start_sim_operator_link(_plan(tmp_path, config), policy)
    args = captured["args"]
    assert args.operator_bind_host == "127.0.0.1" and args.operator_port == 14590
    assert args.operator_requires_approval is True
    # The SIM vehicle hovers before the mission arrives; the gate still sees "landed".
    assert captured["state"].armed is False and captured["state"].landed is True
    assert policy.sim_operator_server == "server"


def test_text_missions_do_not_open_an_operator_endpoint(tmp_path, monkeypatch) -> None:
    import scripts.onboard_main as onboard_main

    monkeypatch.setattr(
        onboard_main, "_start_operator_link", lambda *a: (_ for _ in ()).throw(AssertionError)
    )
    policy = SimpleNamespace()
    collector._start_sim_operator_link(_plan(tmp_path, {"mission": "from_text"}), policy)
    assert not hasattr(policy, "sim_operator_server")


def test_operator_link_is_a_known_mission_template(tmp_path) -> None:
    from onboard_factories import make_policy

    policy = make_policy(work_dir=tmp_path, config={"mission": "operator_link"})
    # Holds safely until a package arrives over the link (SITL e2e failed on
    # "unknown mission template 'operator_link'" before this was accepted).
    assert policy.operator_link_waiting is True


@pytest.mark.usefixtures("require_loopback_sockets")
def test_fake_gcs_console_uploads_prompt_and_approves_the_onboard_plan(tmp_path) -> None:
    import socket
    import threading

    import pytest

    pytest.importorskip("pymavlink")
    from multidetect.autonomy.mission_executor import MissionExecutor
    from multidetect.autonomy.mission_package import load_mission_package
    from multidetect.autonomy.mission_supervisor import MissionSupervisor
    from multidetect.autonomy.onboard_mission_log import DownlinkQueue, OnboardMissionLog
    from scripts import fake_gcs_console
    from scripts.onboard_main import _start_operator_link

    example = json.loads(
        Path("configs/onboard/mission-package-count-cones-example.json").read_text(encoding="utf-8")
    )
    compiled = load_mission_package(example).plan
    prompt_only = {k: v for k, v in example.items() if k != "plan"}
    # Over 20 chunks: onboard drops a burst beyond 20 per message type, which the
    # SITL prompt package (21 chunks) hit while the 16-chunk example did not.
    prompt_only["task_text"] += "；" + "沿场地边缘仔细搜索每一个角落。" * 12
    package_path = tmp_path / "prompt.json"
    package_path.write_text(json.dumps(prompt_only, ensure_ascii=False), encoding="utf-8")
    supervisors = []

    class Policy:
        def admit_operator_package(self, package):
            executor = MissionExecutor(
                compiled, loop_factory=lambda *a, **k: None, refs=package.refs
            )
            log = OnboardMissionLog(tmp_path / "log", package.mission_id)
            # As OnboardPolicy.admit_operator_package: log records (the plan)
            # reach the ground only through a downlink queue.
            supervisor = MissionSupervisor(
                executor,
                package,
                log=log,
                downlink=DownlinkQueue(log, sender=None),
                requires_approval=True,
            )
            supervisors.append(supervisor)
            return supervisor

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    link = {
        "operator_bind_host": "127.0.0.1",
        "operator_port": port,
        "operator_hmac_key_hex": "11" * 32,
        "operator_mavlink_signing_key_hex": "22" * 32,
        "operator_package_key_hex": "33" * 32,
    }
    link_path = tmp_path / "operator-link.json"
    link_path.write_text(json.dumps(link), encoding="utf-8")
    args = SimpleNamespace(**link, operator_requires_approval=True)
    server = _start_operator_link(args, Policy(), collector._SimHoverVehicle())
    try:
        log = tmp_path / "gcs.jsonl"
        console = threading.Thread(
            target=fake_gcs_console.main,
            args=(
                [
                    str(package_path),
                    "--link",
                    str(link_path),
                    "--auto-approve",
                    "--max-seconds",
                    "12",
                    "--log",
                    str(log),
                ],
            ),
        )
        console.start()
        console.join(timeout=15)
    finally:
        server.close()
    kinds = [json.loads(line).get("kind") for line in log.read_text(encoding="utf-8").splitlines()]
    assert "plan" in kinds
    assert supervisors and supervisors[0].plan_approved


@pytest.mark.usefixtures("require_loopback_sockets")
def test_console_gets_a_prompt_finding_and_full_summary_after_a_minute_of_heartbeats(
    tmp_path,
) -> None:
    """Loopback GCS run: >60 s of (simulated) heartbeats and decisions, a finding,
    then an early energy return.  SITL run7 delivered 735 of 2051 records: the
    finding arrived late and no summary reached the console."""
    import socket
    import threading
    import time

    import pytest

    pytest.importorskip("pymavlink")
    from multidetect.autonomy.mission_executor import MissionExecutor
    from multidetect.autonomy.mission_package import load_mission_package
    from multidetect.autonomy.mission_supervisor import MissionSupervisor
    from multidetect.autonomy.onboard_mission_log import DownlinkQueue, OnboardMissionLog
    from scripts import fake_gcs_console
    from scripts.onboard_main import _start_operator_link

    example = json.loads(
        Path("configs/onboard/mission-package-count-cones-example.json").read_text(encoding="utf-8")
    )
    compiled = load_mission_package(example).plan
    prompt_only = {k: v for k, v in example.items() if k != "plan"}
    package_path = tmp_path / "prompt.json"
    package_path.write_text(json.dumps(prompt_only, ensure_ascii=False), encoding="utf-8")
    now = [1000.0]
    supervisors = []

    class Policy:
        def admit_operator_package(self, package):
            executor = MissionExecutor(
                compiled, loop_factory=lambda *a, **k: None, refs=package.refs
            )
            log = OnboardMissionLog(tmp_path / "log", package.mission_id)
            supervisor = MissionSupervisor(
                executor,
                package,
                log=log,
                downlink=DownlinkQueue(log, sender=None),
                requires_approval=True,
                clock=lambda: now[0],
            )
            supervisors.append(supervisor)
            return supervisor

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    link = {
        "operator_bind_host": "127.0.0.1",
        "operator_port": port,
        "operator_hmac_key_hex": "11" * 32,
        "operator_mavlink_signing_key_hex": "22" * 32,
        "operator_package_key_hex": "33" * 32,
    }
    link_path = tmp_path / "operator-link.json"
    link_path.write_text(json.dumps(link), encoding="utf-8")
    args = SimpleNamespace(**link, operator_requires_approval=True)
    gcs_log = tmp_path / "gcs.jsonl"

    def received() -> list[dict]:
        if not gcs_log.exists():
            return []
        lines = gcs_log.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def wait_for(predicate, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return predicate()

    server = _start_operator_link(args, Policy(), collector._SimHoverVehicle())
    console = threading.Thread(
        target=fake_gcs_console.main,
        args=(
            [
                str(package_path),
                "--link",
                str(link_path),
                "--auto-approve",
                "--max-seconds",
                "30",
                "--log",
                str(gcs_log),
            ],
        ),
        daemon=True,
    )
    console.start()
    try:
        assert wait_for(lambda: supervisors and supervisors[0].plan_approved, 15)
        supervisor = supervisors[0]
        mission_id = supervisor.package.mission_id
        lock = supervisor._ground_lock

        def simulated_second() -> None:
            with lock:
                now[0] += 1.0
                assert supervisor.ground_event({"mission_id": mission_id, "type": "heartbeat"})
            supervisor.update_estimates(remaining_energy_wh=80.0)
            supervisor.evaluate()

        for _ in range(75):  # 75 s of mission time: heartbeat, estimates, decision
            simulated_second()
        supervisor.executor.counts["count_cones"] = 1
        supervisor.executor.findings.append(
            {"step": "count_cones", "query": "orange traffic cone", "position_ne": (9.0, -15.0)}
        )
        supervisor._record(
            "finding", step="count_cones", id="f1", label="orange traffic cone", n=9.0, e=-15.0
        )
        found_at = time.monotonic()
        for _ in range(10):
            simulated_second()
        supervisor.update_estimates(remaining_energy_wh=10.0)
        assert supervisor.evaluate() == "return_now"  # energy reserve: early return
        ended_at = time.monotonic()

        assert wait_for(lambda: any(d.get("kind") == "finding" for d in received()), 6), (
            "finding did not reach the console"
        )
        finding_latency = time.monotonic() - found_at
        assert wait_for(lambda: any(d.get("kind") == "final_summary" for d in received()), 8), (
            "final summary did not reach the console"
        )
        summary_latency = time.monotonic() - ended_at
    finally:
        server.close()
    assert finding_latency < 6 and summary_latency < 8
    documents = received()
    summary = next(d for d in documents if d.get("kind") == "final_summary")["summary"]
    assert summary["status"] == "ended_early"
    assert summary["terminal_action"] == "return_now"
    assert "reserve" in summary["terminal_reason"]
    assert [f["query"] for f in summary["findings"]] == ["orange traffic cone"]
    assert summary["counts"] == {"count_cones": 1}
    assert summary["count_is_lower_bound"] is True  # the count step never finished coverage
    kinds = [d.get("kind") for d in documents]
    # Routine traffic stays local: no heartbeat records, no execution/estimates.
    assert not any(
        d.get("kind") == "ground_event" and d.get("event_type") == "heartbeat" for d in documents
    )
    assert "estimates" not in kinds
    assert kinds.count("decision") <= 6
    assert "escalation" in kinds and "status" in kinds
    local = [
        json.loads(line) for line in supervisor.log.path.read_text(encoding="utf-8").splitlines()
    ]
    # Every record is still kept onboard as evidence.
    assert sum(r["kind"] == "ground_event" for r in local) >= 85
    assert sum(r["kind"] == "final_summary" for r in local) == 1


def test_drain_writes_the_summary_and_gets_it_acked_before_shutdown(tmp_path) -> None:
    """policy.close() closes the downlink; draining after it waited 30 s in vain."""
    from multidetect.autonomy.mission_executor import MissionExecutor
    from multidetect.autonomy.mission_package import load_mission_package
    from multidetect.autonomy.mission_supervisor import MissionSupervisor
    from multidetect.autonomy.onboard_mission_log import DownlinkQueue, OnboardMissionLog

    package = load_mission_package(
        json.loads(
            Path("configs/onboard/mission-package-count-cones-example.json").read_text(
                encoding="utf-8"
            )
        )
    )
    log = OnboardMissionLog(tmp_path, package.mission_id)
    received = []
    downlink = DownlinkQueue(log, received.append)
    supervisor = MissionSupervisor(
        MissionExecutor(package.plan, loop_factory=lambda *a, **k: None, refs=package.refs),
        package,
        log=log,
        downlink=downlink,
    )
    events = []

    class Server:
        def close(self):
            events.append(("server_closed", downlink.acknowledged_sequence))

    class Policy:
        sim_operator_server = Server()

        def close(self):
            # As OnboardPolicy.close: finish, then close the downlink.
            supervisor.finish()
            downlink.close()
            events.append(("policy_closed", downlink.acknowledged_sequence))

    policy = Policy()
    policy.supervisor = supervisor
    downlink.set_connected(True)
    collector._drain_sim_operator_link(policy, timeout_s=5.0)
    policy.close()
    (summary,) = [r for r in received if r["kind"] == "final_summary"]
    assert summary["summary"]["status"] == "stopped"
    assert events == [("server_closed", log._sequence), ("policy_closed", log._sequence)]
    assert policy.sim_operator_server is None


def test_drain_closes_the_server_when_no_mission_was_admitted() -> None:
    closed = []
    policy = SimpleNamespace(
        sim_operator_server=SimpleNamespace(close=lambda: closed.append(True)), supervisor=None
    )
    collector._drain_sim_operator_link(policy, timeout_s=0.1)
    assert closed == [True] and policy.sim_operator_server is None


def test_console_approvals_use_fresh_tokens_and_check_the_mission_id() -> None:
    from scripts import fake_gcs_console

    sent = []
    decisions = fake_gcs_console.PlanDecisions(
        "m-1", sent.append, auto_approve=True, prompt=lambda text: None
    )
    decisions.on_plan({"kind": "plan", "mission_id": "other", "plan_sha256": "a" * 64})
    assert sent == []  # a plan for another mission is never approved
    decisions.on_plan({"kind": "plan", "mission_id": "m-1", "plan_sha256": "b" * 64})
    decisions.on_plan({"kind": "plan", "mission_id": "m-1", "plan_sha256": "b" * 64})
    decisions.on_plan({"kind": "plan", "mission_id": "m-1", "plan_sha256": "c" * 64})
    assert [(c["type"], c["plan_sha256"][0]) for c in sent] == [
        ("approve_plan", "b"),
        ("approve_plan", "c"),
    ]
    # The onboard adapter caches completed command transfers by token: a reused
    # token (the console sent 2 every time) returned the cached verdict unexecuted.
    tokens = {fake_gcs_console.new_token() for _ in range(64)}
    assert len(tokens) == 64 and 0 not in tokens


def test_console_waits_for_a_decision_without_blocking_and_signs_commands() -> None:
    from multidetect.autonomy.mission_security import AuthenticationKey, verify_document
    from scripts import fake_gcs_console

    key = AuthenticationKey("hmac-sha256", b"c" * 32, "default")
    sent = []
    decisions = fake_gcs_console.PlanDecisions(
        "m-1",
        sent.append,
        command_key=key,
        command_sequence=7,
        clock=lambda: 1234.5,
        prompt=lambda text: None,
    )
    # on_plan returns at once (it used to block the receive loop in input()).
    decisions.on_plan({"kind": "plan", "mission_id": "m-1", "plan_sha256": "d" * 64})
    decisions.poll()
    assert sent == []
    decisions.inputs.put("r\n")
    decisions.poll()
    (command,) = sent
    assert command["type"] == "reject_plan" and command["sequence"] == 7
    assert command["timestamp_domain"] == "unix" and verify_document(command, key)


def test_console_sequence_starts_from_the_clock_and_package_is_signed() -> None:
    from multidetect.autonomy.mission_package import load_mission_package
    from multidetect.autonomy.mission_security import AuthenticationKey
    from scripts import fake_gcs_console

    # A restarted console starting at 1 was behind the onboard high-water mark.
    assert fake_gcs_console.initial_sequence(1_700_000_000.123) == (1_700_000_000_123 & 0xFFFFFFFF)
    raw = Path("configs/onboard/mission-package-count-cones-example.json").read_bytes()
    key = AuthenticationKey("hmac-sha256", b"p" * 32, "default")
    signed, mission_id = fake_gcs_console.signed_package_bytes(raw, key)
    package = load_mission_package(json.loads(signed), key=key, require_signature=True)
    assert package.mission_id == mission_id
    import pytest

    with pytest.raises(ValueError, match="unsigned"):
        fake_gcs_console.signed_package_bytes(raw, None)


def test_signed_command_sequence_state_advances_and_unsigned_mode_is_inert(tmp_path) -> None:
    from scripts import fake_gcs_console

    state = tmp_path / "command-sequence-state.txt"
    assert fake_gcs_console.load_command_sequence(state, 1) == 1
    fake_gcs_console.persist_command_sequence(state, 7)
    assert fake_gcs_console.load_command_sequence(state, 1) == 8
    assert fake_gcs_console.load_command_sequence(None, 1) == 1
