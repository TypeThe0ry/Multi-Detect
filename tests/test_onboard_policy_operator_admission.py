"""Operator-link packages go through the same admission checks as a start-up package."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from onboard_factories import make_policy

from multidetect.autonomy.mission_package import load_mission_package
from multidetect.autonomy.mission_supervisor import MissionSupervisor

ROOT = Path(__file__).resolve().parents[1]


def _example() -> dict:
    return json.loads(
        (ROOT / "configs/onboard/mission-package-count-cones-example.json").read_text(
            encoding="utf-8"
        )
    )


def _waiting_policy(tmp_path: Path, **config):
    policy = make_policy(work_dir=tmp_path, config={"mission": "operator_link", **config})
    assert policy.operator_link_waiting is True
    return policy


def test_operator_package_plan_is_refused_by_package_policy(tmp_path) -> None:
    policy = _waiting_policy(tmp_path)
    document = _example()
    document["budget"] = dict(document["budget"], energy_wh=1, reserve_energy_wh=0.5)
    package = load_mission_package(document)
    assert package.plan is not None
    package.validate_plan(package.plan)  # the structural check alone admits it
    closed = []
    policy.slow.close = lambda: closed.append(True)
    previous = policy.executor
    with pytest.raises(ValueError, match="ENERGY_BUDGET_EXCEEDED"):
        policy.admit_operator_package(package)
    # Refused without touching the waiting mission or the slow runner.
    assert policy.executor is previous and policy.operator_link_waiting is True
    assert not closed
    records = [
        json.loads(line)
        for path in (tmp_path / "mission_log").rglob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(r.get("kind") == "mission_refused" for r in records)


def test_operator_package_supervisor_uses_the_runtime_clock_and_command_limits(tmp_path) -> None:
    policy = _waiting_policy(
        tmp_path,
        command_rate_limit=7,
        command_rate_window_s=3.0,
        command_timestamp_window_s=12.0,
        command_clock_skew_s=2.0,
    )
    policy.sim_timeline = SimpleNamespace(now=123.0)
    supervisor = policy.admit_operator_package(load_mission_package(_example()))
    assert isinstance(supervisor, MissionSupervisor)
    assert supervisor.clock() == 123.0
    assert (supervisor.rate_limit, supervisor.rate_window_s) == (7, 3.0)
    assert (supervisor.timestamp_window_s, supervisor.clock_skew_s) == (12.0, 2.0)
