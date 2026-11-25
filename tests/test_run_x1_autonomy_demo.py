from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import run_x1_autonomy_demo as demo
from scripts.collect_sim_teacher_episodes import _scenario_channels
from test_x1_planner import _fixture, _plan

WORLD = "rw_farm_iowa_w6km"
SCENARIO_ID = f"{WORLD}__clear_noon"
SCENARIO = (demo.ROOT / "configs/scenarios/realworld" / demo.AIRFRAME / f"{SCENARIO_ID}.scenario.json")
TASK = "Fly over the yard at 85 m AGL, then return home and land."


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    # Use the actual Iowa runtime and DEM. The SDF fixture tests the resolver
    # without depending on external assets or invoking Gazebo.
    with pytest.MonkeyPatch.context() as env:
        env.setenv("MULTIDETECT_RTF_WORLD_ROOT", str(tmp_path))
        world_sdf = tmp_path / f"{SCENARIO_ID}.sdf"
        world_sdf.write_text('<sdf version="1.9"><world name="default"><spherical_coordinates/>'
                             '</world></sdf>', encoding="utf-8")
        from scripts import collect_sim_teacher_episodes as collector
        monkeypatch.setattr(collector, "scenario_world_path", lambda path: world_sdf)
        # The existing URI resolver forbids world overrides outside the repo.
        # For portable tmp_path fixtures, pass the same explicit file to it.
        from multidetect.simulator import stack
        resolve = stack.parse_builtin_world_uri
        monkeypatch.setattr(stack, "parse_builtin_world_uri", lambda uri, **kwargs: resolve(
            uri, world_sdf_path=world_sdf, **kwargs))
        package = _fixture(TASK, target=(320, 0), corridor=(-950, 1100, -950, 950), terrain=(-25, 25)).document
        package["x1_planning"]["world"] = SCENARIO_ID
        package.pop("task_text", None)
        package_input = tmp_path / "package.json"
        package_input.write_text(json.dumps(package), encoding="utf-8")
        response = tmp_path / "response.json"
        args = ["--sentence", TASK, "--world", WORLD, "--scenario", str(SCENARIO),
                "--mission-package", str(package_input), "--output", str(tmp_path / "result"),
                "--recorded-output", str(response)]
        yield response, args, tmp_path / "result" / "result.json"


def raw_plan(*, ref="poi:yard", altitude=85):
    plan = _plan(altitude=altitude)
    plan["steps"][0]["params"]["altitude_agl_m"] = altitude
    plan["steps"][2]["params"]["target_ref"] = ref
    return json.dumps(plan)


@pytest.mark.parametrize("flag", ["--plan-only", "--dry-run"])
def test_prepare_recorded_cli(inputs, flag):
    response, argv, result = inputs
    response.write_text(raw_plan(), encoding="utf-8")
    assert demo.main([*argv, flag]) == 0
    record = json.loads(result.read_text(encoding="utf-8"))
    assert record["sentence"] == TASK
    assert record["validator"]["verdict"] == "accepted"
    assert record["plan_json"] == json.loads(raw_plan())
    assert len(record["mission_package_sha256"]) == 64
    assert record["flight"]["status"] == "plan_only"
    assert record["flight"]["takeoff"] is None
    assert record["prepared"]["collector"]["image"] == demo.IMAGE
    assert record["prepared"]["collector"]["steps_per_episode"] == 4
    assert record["prepared"]["collector"]["runtime"] == str(_scenario_channels(SCENARIO)[0])
    assert [row["primitive"] for row in record["prepared"]["steps"]] == [
        "vtol_takeoff", "fw_transition", "fw_goto_waypoint", "back_transition", "mc_return_to_home", "vtol_land",
    ]
    target = record["prepared"]["steps"][2]["command"]["target_ned"]
    assert target[:2] == [320.0, 0.0]
    assert -120 <= target[2] <= -40


@pytest.mark.parametrize(("response_text", "code", "reason"), [
    (raw_plan(altitude=121), "INVALID_PLAN", "altitude_agl_m"),
    ('{"plan_version":', "INVALID_PLAN", "JSON"),
])
def test_rejected_model_output(inputs, response_text, code, reason):
    response, argv, result = inputs
    response.write_text(response_text, encoding="utf-8")
    assert demo.main([*argv, "--plan-only"]) == 1
    record = json.loads(result.read_text(encoding="utf-8"))
    assert record["validator"]["verdict"] == "rejected"
    assert record["validator"]["code"] == code
    assert reason in record["validator"]["reason"]
    assert record["flight"]["status"] == "not_started"
    assert "prepared" not in record


def test_planner_timeout(inputs, monkeypatch):
    _response, argv, result = inputs
    calls = []

    def timeout(prompt, schema):
        calls.append((prompt, schema))
        raise TimeoutError("planner deadline")

    monkeypatch.setattr(demo, "recorded_backend", lambda _: timeout)
    assert demo.main([*argv, "--dry-run"]) == 1
    record = json.loads(result.read_text(encoding="utf-8"))
    assert record["validator"]["code"] == "PLANNER_TIMEOUT"
    assert "planner deadline" in record["validator"]["reason"]
    assert record["flight"]["status"] == "not_started"
    assert len(calls) == 1 and TASK in calls[0][0]
    assert "fw_goto_waypoint" in json.dumps(calls[0][1])


def test_recorded_backend_cannot_fly(inputs):
    _response, argv, result = inputs
    with pytest.raises(SystemExit) as exc:
        demo.main(argv)
    assert exc.value.code == 2
    assert not result.exists()


def test_waypoint_comes_from_output(inputs):
    response, argv, result = inputs
    package_path = Path(argv[argv.index("--mission-package") + 1])
    package = json.loads(package_path.read_text(encoding="utf-8"))
    package["places"]["poi:field"] = [200, 250]
    package_path.write_text(json.dumps(package), encoding="utf-8")
    response.write_text(raw_plan(ref="poi:field"), encoding="utf-8")
    # The second packaged POI resolves to a different approach and NED target.
    argv[argv.index("--sentence") + 1] = "Fly over the field at 85 m AGL, then return home and land."
    assert demo.main([*argv, "--dry-run"]) == 0
    prepared = json.loads(result.read_text(encoding="utf-8"))["prepared"]
    assert prepared["steps"][2]["command"]["target_ned"][:2] == [200, 250]


def test_out_of_envelope_runout(inputs):
    response, argv, result = inputs
    package_path = Path(argv[argv.index("--mission-package") + 1])
    package = json.loads(package_path.read_text(encoding="utf-8"))
    package["x1_planning"]["max_distance_from_home_m"] = 500
    package_path.write_text(json.dumps(package), encoding="utf-8")
    response.write_text(raw_plan(), encoding="utf-8")
    assert demo.main([*argv, "--dry-run"]) == 1
    record = json.loads(result.read_text(encoding="utf-8"))
    assert record["validator"]["code"] == "X1_DISTANCE"
    assert "maximum distance from home" in record["validator"]["reason"]
    assert record["validator"]["findings"]
    assert record["flight"]["status"] == "not_started"


def test_flight_receipt_requires_passage_and_landed_mc(inputs):
    from types import SimpleNamespace

    response, argv, result = inputs
    response.write_text(raw_plan(), encoding="utf-8")
    options = dict(zip(argv[::2], argv[1::2], strict=True))
    args = SimpleNamespace(sentence=TASK, world=WORLD, scenario=SCENARIO,
                           mission_package=Path(options["--mission-package"]),
                           output=result.parent, plan_only=False, slot=7)

    def fake_launch(_args, collector_plan, steps, mission, _record):
        commands = tuple(demo.MissionPolicy(steps, -56).commands)
        assert len(commands) == collector_plan.steps_per_episode
        mission["x1_cruise_guard"](steps[2].command.target_ned)
        episode = Path(collector_plan.out) / "episodes" / "test"
        episode.mkdir(parents=True)
        (episode / "episode.json").write_text('{"status":"complete"}', encoding="utf-8")
        rows = [{"message_type": "fixed_wing_takeoff_complete"},
                {"message_type": "collector_landing_complete", "flight_state": {"vtol_state": 3, "landed_state": 1}}]
        (episode / "mavlink.jsonl").write_text("\n".join(map(json.dumps, rows)), encoding="utf-8")
        motions = [{"labels": {"motion": command.to_dict()}, "outcome": {"status": "success"},
                    "ack": {"motion_wait": {"waited": state, **evidence}}}
                   for command, state, evidence in zip(commands, ["transitioned", "arrived", "transitioned", "settled"],
                       [{}, {"x1_passage": {"crossing_cross_track_m": 0.0}}, {}, {}], strict=True)]
        (episode / "steps.jsonl").write_text("\n".join(map(json.dumps, motions)), encoding="utf-8")
        return episode

    assert demo.run(args, post=demo.recorded_backend(response), launch=fake_launch) == 0
    record = json.loads(result.read_text(encoding="utf-8"))
    assert record["flight"]["status"] == "pass"
    assert record["flight"]["waypoints_reached"] == ["w"]
    assert record["flight"]["takeoff"] and record["flight"]["return_home"] and record["flight"]["landing"]
    assert record["safety"]["observed"]["sample_count"] == 1
    assert record["safety"]["observed"]["minimum_margins_m"]["min_agl_m"] == pytest.approx(45)
