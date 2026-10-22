from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from scripts import collect_sim_teacher_episodes as collector


def _plan(tmp_path: Path, config: dict) -> SimpleNamespace:
    path = tmp_path / "onboard.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return SimpleNamespace(onboard_config=str(path))


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
                    "8",
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
