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


def test_operator_package_supervisor_gets_the_runtime_energy_model(tmp_path) -> None:
    policy = _waiting_policy(tmp_path, energy_model={"hover_power_w": 777, "battery_id": 3})
    supervisor = policy.admit_operator_package(load_mission_package(_example()))
    config = supervisor.energy_estimator.config
    assert (config.hover_power_w, config.battery_id) == (777, 3)


def test_prepared_operator_package_is_installed_only_on_request(tmp_path) -> None:
    policy = _waiting_policy(tmp_path)
    previous = policy.executor
    candidate = policy.prepare_operator_package(load_mission_package(_example()))
    assert isinstance(candidate, MissionSupervisor)
    # Preparing (the slow, worker-thread step) leaves the waiting mission alone.
    assert policy.executor is previous and policy.supervisor is None
    assert policy.operator_link_waiting is True
    policy.install_operator_package(candidate)
    assert policy.executor is candidate and policy.supervisor is candidate
    assert policy.downlink is candidate.downlink and policy.operator_link_waiting is False


def _prompt_package() -> dict:
    """The e2e run10 prompt package: a Chinese task and one labelled zone, no plan."""
    document = _example()
    document.pop("plan", None)
    document.update(
        task_text="数一数场地里有几个橙色交通锥，报告位置后返航",
        zones={
            "zone:site": {
                "centre_ne": [0, 0],
                "radius_m": 55,
                "risk_tag": "open_field",
                "oversight_required": False,
            }
        },
        ref_labels={"zone:site": "场地 / the site"},
        operating_area=dict(
            document["operating_area"],
            north_min_m=-100, north_max_m=100, east_min_m=-100, east_max_m=100,
        ),
    )
    return document


def _fake_planner(monkeypatch) -> list[str]:
    """A planner that, like r18/r14 on run10's task, finds the zone only by its label."""
    import scripts.eval_slow_system as slow_system

    prompts: list[str] = []

    def http_json(url, body, timeout=None):
        prompt = body["messages"][0]["content"]
        prompts.append(prompt)
        region = "zone:site" if "zone:site (场地 / the site)" in prompt else "zone:all"
        plan = {
            "goal": "Count the orange traffic cones, report positions and return",
            "plan_version": 1,
            "report": ["count", "positions"],
            "steps": [
                {
                    "id": "s1", "on_fail": "s2", "on_success": "s2",
                    "params": {"query": "orange traffic cone", "region": region},
                    "skill": "count",
                },
                {"id": "s2", "on_fail": None, "on_success": None, "params": {},
                 "skill": "return_home"},
            ],
        }
        return {"choices": [{"message": {"content": json.dumps(plan)}}]}

    monkeypatch.setattr(slow_system, "_http_json", http_json)
    return prompts


def test_operator_task_is_planned_with_the_package_reference_labels(
    tmp_path, monkeypatch
) -> None:
    # run10: the operator path listed bare "zone:site", the planner chose
    # zone:all and the count searched a 150 m square until its budget ran out.
    prompts = _fake_planner(monkeypatch)
    policy = _waiting_policy(tmp_path)
    supervisor = policy.admit_operator_package(load_mission_package(_prompt_package()))
    assert "Known places: zone:site (场地 / the site)." in prompts[0]
    assert supervisor.executor.plan.steps[0].params["region"] == "zone:site"
    assert supervisor.executor.region_of(supervisor.executor.plan.steps[0]) == (0.0, 0.0, 55.0)


def test_operator_task_sizes_the_search_and_titles_the_mission(tmp_path, monkeypatch) -> None:
    # The policy starts on a placeholder sentence; the package's cone task must
    # size the search as the suite's start-up task does (2 m default -> 0.5 m cone).
    _fake_planner(monkeypatch)
    policy = _waiting_policy(tmp_path, size_aware_search=True, task_text="operator link mission")
    placeholder_altitude = policy.search_altitude_m
    assert placeholder_altitude == pytest.approx(20.0)
    supervisor = policy.admit_operator_package(load_mission_package(_prompt_package()))
    assert policy.task_text == "数一数场地里有几个橙色交通锥，报告位置后返航"
    assert policy.search_altitude_m == pytest.approx(8.0)
    assert policy._search is policy._search_mc
    assert policy._search_mc.altitude_m == pytest.approx(8.0)
    loop = supervisor.executor.loop_factory("orange traffic cone")
    assert loop.config.search_altitude_m == pytest.approx(8.0)
    assert loop.config.ground_extent_size_m == pytest.approx(0.5)
