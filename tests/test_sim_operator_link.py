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
