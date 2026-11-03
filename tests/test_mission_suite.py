from __future__ import annotations

import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from multidetect.runtime import RuntimeMode
from multidetect.simulation import load_runtime_scenario
from multidetect.simulation.index import load_scenario_index, load_verified_channel
from multidetect.simulator.stack import resolve_simulator_assets
from scripts import augment_sim_world as augment
from scripts import collect_sim_teacher_episodes as collector
from scripts import run_mission_suite as suite


@pytest.fixture
def work_tmp():
    path = suite.ROOT / ".pytest-mission-suite" / uuid.uuid4().hex
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _mission(**overrides):
    mission = {
        "id": "count-cones",
        "task_text": "Count cones at the site.",
        "index": "configs/scenarios/generated-count-site/index.json",
        "world": "gen_cnt2_w0000",
        "refs": {"zone": "site"},
        "ref_labels": {"zone": "site"},
        "expect": {"skills": ["survey", "count"], "min_alert_precision": 0.8},
        "steps": 42,
        "max_episode_s": 120,
    }
    mission.update(overrides)
    return mission


def test_merge_config_deep_copies_and_excludes_suite_metadata():
    defaults = {
        "gguf": "model.gguf",
        "mission": "from_text",
        "plan_sorted_keys": True,
        "refs": {"base": "default"},
        "ref_labels": {"base": "Base"},
    }
    mission = _mission(refs={"zone": "site", "nested": {"x": 1}}, ref_labels={"zone": "Zone"})
    merged = suite.merge_config(defaults, mission)

    assert merged["gguf"] == "model.gguf"
    assert merged["mission"] == "from_text"
    assert merged["refs"] == {"base": "default", "zone": "site", "nested": {"x": 1}}
    assert merged["ref_labels"] == {"base": "Base", "zone": "Zone"}
    excluded = ("id", "index", "world", "expect", "steps", "max_episode_s")
    assert all(key not in merged for key in excluded)
    mission["refs"]["nested"]["x"] = 9
    assert merged["refs"]["nested"]["x"] == 1


def test_build_command_uses_same_interpreter_and_absolute_paths(work_tmp):
    mission = _mission()
    command = suite.build_command(
        mission,
        work_tmp / "onboard.json",
        work_tmp / "campaign",
        work_tmp / "report.json",
        episodes=3,
    )

    assert command[0] == sys.executable
    assert command[1] == str(suite.ROOT / "scripts/eval_onboard_loop_campaign.py")
    assert command[command.index("--index") + 1] == str((suite.ROOT / mission["index"]).resolve())
    assert command[command.index("--split") + 1] == "test"
    assert command[command.index("--worlds") + 1] == mission["world"]
    assert command[command.index("--episodes") + 1] == "3"
    assert command[command.index("--steps") + 1] == "42"
    assert command[command.index("--max-episode-s") + 1] == "120"


def test_k1_command_keeps_legacy_slot_arguments_absent(work_tmp):
    command = suite.build_command(_mission(), work_tmp / "onboard.json", work_tmp / "campaign", work_tmp / "report.json")
    assert not {"--control-port", "--telemetry-port", "--container-prefix", "--operator-port"} & set(command)


def test_worker_dry_run_assigns_unique_ports_and_container_names(work_tmp, monkeypatch, capsys):
    monkeypatch.setenv("MULTIDETECT_SITL_SLOTS", "2")
    suite_path = work_tmp / "suite.json"
    suite_path.write_text(json.dumps({"defaults": {"mission": "from_text"}, "missions": [_mission(id="one"), _mission(id="two", world="gen_cnt2_w0001")]}), encoding="utf-8")
    assert suite.main(["--suite", str(suite_path), "--out", str(work_tmp / "out"), "--workers", "2", "--dry-run"]) == 0
    output = capsys.readouterr().out
    decoder = json.JSONDecoder()
    rows = []
    offset = 0
    while offset < len(output):
        while offset < len(output) and output[offset].isspace():
            offset += 1
        if offset == len(output):
            break
        row, offset = decoder.raw_decode(output, offset)
        rows.append(row)
    commands = [row["command"] for row in rows]
    controls = [command[command.index("--control-port") + 1] for command in commands]
    telemetry = [command[command.index("--telemetry-port") + 1] for command in commands]
    prefixes = [command[command.index("--container-prefix") + 1] for command in commands]
    operators = [command[command.index("--operator-port") + 1] for command in commands]
    model_ports = [row["config"]["port"] for row in rows]
    assert len(set(controls)) == len(set(telemetry)) == len(set(prefixes)) == 2
    assert len(set(operators)) == len(set(model_ports)) == 2


def test_build_table_maps_metrics_and_missing_episode_is_failure():
    mission = _mission()
    report = {
        "episodes": [
            {
                "world": mission["world"],
                "status": "complete",
                "mission_plan_skills": ["survey", "count"],
                "mission_completed": True,
                "mission_alert_precision": 0.9,
                "mission_alert_recall": 0.75,
                "expected_count": 4,
                "reported_count": 4,
                "matched_count": 4,
                "false_findings": 0,
                "mission_inspect_match_fraction": 1.0,
                "mission_survey_coverage": {"site": 0.8},
                "steps": 42,
            }
        ]
    }
    table = suite.build_table([mission], {mission["id"]: report})
    row = table["missions"][0]
    assert row["plan_match"] is True
    assert row["mission_completed"] is True
    assert row["alert_precision"] == pytest.approx(0.9)
    assert row["alert_recall"] == pytest.approx(0.75)
    assert row["expected_count"] == 4
    assert row["reported_count"] == 4
    assert row["matched_count"] == 4
    assert row["false_findings"] == 0
    assert row["inspect_match_fraction"] == pytest.approx(1.0)
    assert row["survey_coverage"] == {"site": 0.8}
    assert table["summary"]["plan_accuracy"] == 1.0
    assert table["summary"]["completion_rate"] == 1.0

    missing = suite.build_table([_mission(id="missing", world="gen_cnt2_w0001")], {})
    missing_row = missing["missions"][0]
    assert missing_row["status"] == "missing"
    assert missing_row["plan_match"] is False
    assert missing_row["mission_completed"] is False
    assert missing["summary"]["plan_accuracy"] == 0.0
    assert missing["summary"]["completion_rate"] == 0.0


def test_listed_alternative_plan_passes_all_expectations():
    mission = _mission(
        expect={
            "skills": ["area_search", "land"],
            "alternatives": [["area_search", "record", "land"]],
        }
    )
    episode = {
        "world": mission["world"],
        "status": "complete",
        "mission_plan_skills": ["area_search", "record", "land"],
        "mission_completed": True,
    }
    row = suite.build_table([mission], {mission["id"]: {"episodes": [episode]}})["missions"][0]
    assert row["plan_match"] is True
    assert row["expectation_checks"] == {"skills": True}
    assert row["expectations_passed"] is True


def _write_suite(path: Path):
    payload = {
        "defaults": {"gguf": "model.gguf", "plan_sorted_keys": True},
        "missions": [_mission(), _mission(id="count-hydrants", world="gen_cnt2_w0001")],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_main_only_filters_and_runner_receives_expected_command(work_tmp):
    suite_path = work_tmp / "suite.json"
    _write_suite(suite_path)
    calls = []

    def fake_runner(command, *, cwd, check):
        calls.append((command, cwd, check))
        report_path = Path(command[command.index("--report") + 1])
        report_path.write_text(json.dumps({"episodes": []}), encoding="utf-8")

    code = suite.main(
        ["--suite", str(suite_path), "--out", str(work_tmp / "out"), "--only", "count-hydrants"],
        runner=fake_runner,
    )
    assert code == 1  # no episode was reported, so the selected mission is a failure
    assert len(calls) == 1
    assert calls[0][1] == suite.ROOT
    assert calls[0][2] is True
    assert "gen_cnt2_w0001" in calls[0][0]


def test_parallel_worker_reports_are_aggregated(work_tmp, monkeypatch):
    monkeypatch.setenv("MULTIDETECT_SITL_SLOTS", "2")
    suite_path = work_tmp / "suite.json"
    _write_suite(suite_path)

    def fake_runner(command, *, cwd, check):
        world = command[command.index("--worlds") + 1]
        Path(command[command.index("--report") + 1]).write_text(
            json.dumps({"episodes": [{"world": world, "status": "complete", "mission_completed": True}]}),
            encoding="utf-8",
        )

    assert suite.main(
        ["--suite", str(suite_path), "--out", str(work_tmp / "parallel"), "--workers", "2"],
        runner=fake_runner,
    ) == 0
    report = json.loads((work_tmp / "parallel/suite-report.json").read_text(encoding="utf-8"))
    assert [row["id"] for row in report["missions"]] == ["count-cones", "count-hydrants"]
    assert report["summary"]["completion_rate"] == 1.0


def test_main_dry_run_writes_configs_and_does_not_call_runner(work_tmp, capsys):
    suite_path = work_tmp / "suite.json"
    _write_suite(suite_path)
    calls = []

    def fake_runner(*args, **kwargs):
        calls.append((args, kwargs))

    out = work_tmp / "dry-run"
    args = ["--suite", str(suite_path), "--out", str(out), "--dry-run"]
    assert suite.main(args, runner=fake_runner) == 0
    assert calls == []
    assert (out / "count-cones" / "onboard.json").is_file()
    assert (out / "count-hydrants" / "onboard.json").is_file()
    assert not (out / "suite-report.json").exists()
    output = capsys.readouterr().out
    assert '"command"' in output
    assert '"config"' in output


def test_variant_generates_world_before_runner_and_labels_report(work_tmp):
    mission = _mission(
        id="count-cones-wind",
        variant={"wind_mps": 6, "gust_mps": 3, "gust_period": 4},
    )
    suite_path = work_tmp / "variant-suite.json"
    suite_path.write_text(json.dumps({"defaults": {}, "missions": [mission]}), encoding="utf-8")
    generated = []
    calls = []

    def fake_generator(command, *, cwd, check):
        generated.append((command, cwd, check))

    def fake_runner(command, *, cwd, check):
        calls.append(command)
        world = command[command.index("--worlds") + 1]
        report = {"episodes": [{"world": world, "status": "complete"}]}
        Path(command[command.index("--report") + 1]).write_text(
            json.dumps(report), encoding="utf-8"
        )

    assert suite.main(
        ["--suite", str(suite_path), "--out", str(work_tmp / "out")],
        runner=fake_runner,
        generator_runner=fake_generator,
    ) == 0
    assert len(generated) == 1
    assert "--wind-mps" in generated[0][0] and "--gust-mps" in generated[0][0]
    assert calls[0][calls[0].index("--worlds") + 1] == "gen_cnt2_w0000__wind6_gust3p4"
    table = json.loads((work_tmp / "out/suite-report.json").read_text(encoding="utf-8"))
    assert table["missions"][0]["variant"] == "wind-mps-6+gust-mps-3+gust-period-4"


@pytest.mark.onboard
def test_copy_variant_index_uses_explicit_windows_channel_bindings(work_tmp):
    original = suite.ROOT / "configs/scenarios/generated-count-site"
    world = "gen_cnt2_w0000"
    source = work_tmp / "source"
    bundle = source / "bundle"
    channels = bundle / "channels"
    channels.mkdir(parents=True)
    manifest = json.loads((original / f"{world}.scenario.json").read_text(encoding="utf-8"))
    for name, binding in manifest["channels"].items():
        # The runtime filename need not use the canonical runtime_input suffix.
        filename = "runtime.json" if name == "runtime_input" else f"{name}.json"
        shutil.copyfile(original / binding["path"], channels / filename)
        binding["path"] = f"channels\\{filename}"
    suite.write_json(bundle / "manifest.json", manifest)
    suite.write_json(source / "index.json", {"worlds": [{
        "world_id": world, "world_file": f"{world}.sdf", "split": "test",
        "files": {"scenario": {"path": "bundle\\manifest.json"}},
    }]})
    destination = work_tmp / "output/scenario-index.json"

    suite._copy_variant_index(source / "index.json", destination, world=world)

    scenario, staged = load_scenario_index(destination.parent / f"{world}.scenario.json")
    for name in ("runtime_input", "truth", "expectations"):
        assert getattr(staged, name).path == f"{world}.{name}.json"
        channel = load_verified_channel(scenario, getattr(staged, name), name=name)
        assert channel["scenario_id"] == world
    assert json.loads((bundle / "manifest.json").read_text(encoding="utf-8")) == manifest


@pytest.mark.onboard
def test_missing_source_channel_fails_before_publishing_variant_index(work_tmp):
    original = suite.ROOT / "configs/scenarios/generated-count-site"
    source = work_tmp / "source"
    shutil.copytree(original, source)
    (source / "gen_cnt2_w0000.truth.json").unlink()
    destination = work_tmp / "output/scenario-index.json"

    with pytest.raises(FileNotFoundError, match="gen_cnt2_w0000.truth.json"):
        suite._copy_variant_index(source / "index.json", destination, world="gen_cnt2_w0000")

    assert not destination.exists()


@pytest.mark.onboard
@pytest.mark.parametrize("variant,world", [
    ({"wind_mps": 6, "gust_mps": 3}, "gen_cnt2_w0000__wind6_gust3p5"),
    ({"wind_mps": 0, "camera_noise": 0}, "gen_cnt2_w0000__wind0_noise0"),
    ({"time_of_day": "dusk"}, "gen_cnt2_w0000__dusk"),
])
def test_variant_dry_run_and_generator_use_same_world_id(
    work_tmp, capsys, monkeypatch, variant, world
):
    suite_path = work_tmp / "suite.json"
    suite.write_json(suite_path, {"defaults": {}, "missions": [_mission(variant=variant)]})

    def no_run(*args, **kwargs):
        pytest.fail("dry-run must not start the collector or generator")

    assert suite.main(
        ["--suite", str(suite_path), "--out", str(work_tmp / "out"), "--dry-run"],
        runner=no_run, generator_runner=no_run,
    ) == 0
    row = json.loads(capsys.readouterr().out)
    assert row["command"][row["command"].index("--worlds") + 1] == world
    # Run the printed generator command offline with all outputs isolated.
    command = row["variant_command"]
    command[command.index("--out-dir") + 1] = str(work_tmp / "worlds")
    register_position = command.index("--register-index")
    command = command[:register_position] + command[register_position + 2:]
    monkeypatch.setattr(sys, "argv", command[1:])
    assert augment.main() == 0
    assert (work_tmp / "worlds" / f"{world}.sdf").is_file()


@pytest.mark.onboard
@pytest.mark.parametrize("name", ["robustness", "follow", "airframes"])
@pytest.mark.parametrize("version", ["", "-r21idr3-r24plan"])
def test_suite_scenario_channels_are_ready_before_collector(
    work_tmp, monkeypatch, name, version
):
    # Run the real augmenter against an isolated repository. Never publish test
    # worlds into the checkout or the shared artifacts symlink.
    root = work_tmp / "repo"
    source_root = suite.ROOT
    suite_path = source_root / f"configs/onboard/suite-{name}{version}.json"
    document = json.loads(suite_path.read_text(encoding="utf-8"))
    for mission in document["missions"]:
        source_index = source_root / mission["index"]
        destination = root / mission["index"]
        if not destination.parent.exists():
            shutil.copytree(source_index.parent, destination.parent)
        for directory, suffix in (
            ("gazebo/worlds", ".sdf"), ("maps", ".geojson"), ("terrain", ".dem.json")
        ):
            relative = Path("simulation") / directory / f"{mission['world']}{suffix}"
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_root / relative, target)
    shutil.copytree(source_root / "simulation/gazebo/models", root / "simulation/gazebo/models")
    (root / "scripts").mkdir()
    shutil.copyfile(
        source_root / "scripts/gazebo_sensor_stream.py", root / "scripts/gazebo_sensor_stream.py"
    )
    monkeypatch.setattr(suite, "ROOT", root)
    monkeypatch.setattr(augment, "ROOT", root)
    monkeypatch.setattr(collector, "ROOT", root)
    monkeypatch.setenv("MULTIDETECT_SIM_LISTEN_HOST", "127.0.0.1")
    monkeypatch.setenv("MULTIDETECT_SIM_GPU", "0")
    # Relative --out must be resolved from the caller's cwd, whereas the
    # generator/collector run with cwd=ROOT (as in a pinned nightly worktree).
    monkeypatch.chdir(work_tmp)
    generated, collected = [], []

    def generate(command, *, cwd, check):
        assert cwd == root and check is True
        generated.append(command)
        monkeypatch.setattr(sys, "argv", command[1:])
        assert augment.main() == 0

    def collect(command, *, cwd, check):
        assert cwd == root and check is True
        index = Path(command[command.index("--index") + 1])
        world = command[command.index("--worlds") + 1]
        entries = json.loads(index.read_text(encoding="utf-8"))["worlds"]
        entry = next(row for row in entries if row["world_id"] == world)
        assert entry["split"] == command[command.index("--split") + 1]
        scenario, manifest = load_scenario_index(index.parent / f"{world}.scenario.json")
        assert manifest.scenario_id == world
        for name in ("runtime_input", "truth", "expectations"):
            channel = load_verified_channel(scenario, getattr(manifest, name), name=name)
            assert channel["scenario_id"] == world
        runtime = load_runtime_scenario(scenario, mode=RuntimeMode.SIM)
        assets = resolve_simulator_assets(runtime, repository_root=root)
        assert assets.world.path.is_file()
        if entry.get("variant_of"):
            assert set(entry["files"]) == {
                "scenario", "runtime_input", "truth", "expectations", "world"
            }
            for binding in entry["files"].values():
                assert (index.parent / binding["path"]).is_file(), binding
            local_sdf = index.parent / entry["world_file"]
            assert local_sdf.read_bytes() == assets.world.path.read_bytes()
            sdf = ET.parse(local_sdf).getroot().find("world")
            mission = next(row for row in document["missions"] if row["id"] == index.parent.name)
            variant = mission["variant"]
            if variant.get("time_of_day") == "dusk":
                assert sdf.findtext("light/intensity") == "0.3"
                assert sdf.findtext("scene/ambient") == "0.12 0.14 0.2 1"
            if "wind_mps" in variant:
                assert sdf.findtext("wind/linear_velocity") == "6 0 0"
                assert "amplitude_percent=0.5 period=4" in local_sdf.read_text(encoding="utf-8")
            if variant.get("actors"):
                assert len(sdf.findall("actor")) == variant["actors"]
                assert (index.parent / "actor_box.dae").is_file()
            original_world = Path(entry["variant_of"]).stem
            original_entry = next(row for row in entries if row["world_id"] == original_world)
            assert entry["split"] == original_entry["split"]
            # Target/trajectory truth and expectations stay identical except
            # for the variant identity; weather must not regenerate targets.
            for name in ("truth", "expectations"):
                actual_path = index.parent / f"{world}.{name}.json"
                expected_path = index.parent / f"{original_world}.{name}.json"
                actual = json.loads(actual_path.read_text(encoding="utf-8"))
                expected = json.loads(expected_path.read_text(encoding="utf-8"))
                assert actual == {**expected, "scenario_id": world}
            # The real collector must also be able to plan a launch when --out
            # is outside its pinned worktree. Planning starts no Docker process.
            runtime_path, truth_path = collector._scenario_channels(scenario)
            plan = collector.CollectionPlan(
                str(scenario), str(runtime_path), str(truth_path), "onboard", 1, 1, 0,
                str(index.parent / "campaign"),
            )
            docker_command = collector.docker_command(plan)
            assert any(f"source={assets.world.path}," in token for token in docker_command)
        collected.append(world)
        Path(command[command.index("--report") + 1]).write_text(
            json.dumps({"episodes": [{"world": world, "status": "complete"}]}),
            encoding="utf-8",
        )

    assert suite.main(
        ["--suite", str(suite_path), "--out", "out"],
        runner=collect,
        generator_runner=generate,
    ) == 0
    assert len(collected) == len(document["missions"])
    assert len(generated) == sum(bool(m.get("variant")) for m in document["missions"])


def test_default_mission_does_not_invoke_variant_generator(work_tmp):
    suite_path = work_tmp / "default-suite.json"
    _write_suite(suite_path)
    generated = []

    def fake_generator(*args, **kwargs):
        generated.append((args, kwargs))

    def fake_runner(command, *, cwd, check):
        Path(command[command.index("--report") + 1]).write_text(
            json.dumps({"episodes": []}), encoding="utf-8"
        )

    suite.main(
        ["--suite", str(suite_path), "--out", str(work_tmp / "out")],
        runner=fake_runner,
        generator_runner=fake_generator,
    )
    assert generated == []


def _supervised_mission():
    return _mission(
        expect={
            "skills": ["monitor", "return_home"],
            "supervisor": {
                "final_decision": "return_now",
                "reason": "explicit operator abort",
                "return_home_started": True,
                "onboard_log_exists": True,
                "logged_decisions": ["continue", "return_now"],
                "logged_reasons": ["explicit operator abort"],
                "abort_hover_before_return": True,
                "original_mission_completed": False,
            },
        }
    )


def _supervised_episode(mission):
    return {
        "world": mission["world"],
        "status": "complete",
        "mission_plan_skills": ["monitor", "return_home"],
        "mission_supervisor": {
            **mission["expect"]["supervisor"],
            "logged_decisions": ["continue", "hold", "return_now"],
        },
    }


def test_supervisor_expectations_check_durable_decisions_and_recovery():
    mission = _supervised_mission()
    episode = _supervised_episode(mission)
    row = suite.build_table([mission], {mission["id"]: {"episodes": [episode]}})["missions"][0]
    assert row["expectations_passed"] is True
    assert row["expectation_checks"]["supervisor.original_mission_completed"] is True
    assert row["supervisor"]["original_mission_completed"] is False
    markdown = suite.render_markdown(
        {
            "summary": {
                "missions": 1,
                "episodes": 1,
                "plan_accuracy": 1,
                "completion_rate": 0,
                "expectation_pass_rate": 1,
            },
            "missions": [row],
        }
    )
    assert "Supervisor outcomes" in markdown
    assert "explicit operator abort" in markdown


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("final_decision", "continue"),
        ("reason", "operator link-loss policy"),
        ("return_home_started", False),
        ("onboard_log_exists", False),
        ("logged_decisions", ["continue"]),
        ("logged_reasons", []),
        ("abort_hover_before_return", False),
        ("original_mission_completed", True),
    ],
)
def test_supervisor_expectation_failures_are_visible(field, value):
    mission = _supervised_mission()
    episode = _supervised_episode(mission)
    episode["mission_supervisor"][field] = value
    row = suite.build_table([mission], {mission["id"]: {"episodes": [episode]}})["missions"][0]
    assert row["expectation_checks"][f"supervisor.{field}"] is False
    assert row["expectations_passed"] is False


def test_missing_supervisor_evidence_fails_even_false_expectations():
    mission = _supervised_mission()
    episode = _supervised_episode(mission)
    episode.pop("mission_supervisor")
    row = suite.build_table([mission], {mission["id"]: {"episodes": [episode]}})["missions"][0]
    assert row["expectations_passed"] is False
    assert not any(v for k, v in row["expectation_checks"].items() if k.startswith("supervisor."))


def test_v2_retains_the_eight_v1_missions_and_adds_six_packages(work_tmp, capsys):
    v1 = json.loads((suite.ROOT / "configs/onboard/mission-suite-v1.json").read_text("utf-8"))
    v2 = json.loads((suite.ROOT / "configs/onboard/mission-suite-v2.json").read_text("utf-8"))
    assert v2["defaults"] == v1["defaults"]
    assert v2["missions"][:8] == v1["missions"]
    assert len(v2["missions"]) == 14
    assert all("mission_package" in m for m in v2["missions"][8:])
    count = v2["missions"][8]
    assert count["sim_ground_events"] == []
    assert count["mission_package"]["link_loss_policy"] == {"action": "continue"}
    assert suite.main(["--out", str(work_tmp / "default-v2"), "--dry-run"]) == 0
    assert len(list((work_tmp / "default-v2").glob("*/onboard.json"))) == 14
    capsys.readouterr()


def test_default_suite_uses_r12_identity_and_r14_planner(work_tmp, capsys):
    config = json.loads(
        (suite.ROOT / "configs/onboard/mission-suite-v2-r12id-r14plan.json").read_text(
            encoding="utf-8"
        )
    )
    assert config["defaults"]["gguf"].endswith(
        "slow-system-lora-v2-r12-multi-merged-gguf/slow-system-v2r12multi-merged-Q5_K_M.gguf"
    )
    assert config["defaults"]["planning_model"].endswith(
        "slow-system-lora-v2-r14-multi-merged-gguf/slow-system-v2r14multi-merged-Q5_K_M.gguf"
    )
    assert suite.main(["--out", str(work_tmp / "default-r12-r14"), "--dry-run"]) == 0
    assert len(list((work_tmp / "default-r12-r14").glob("*/onboard.json"))) == len(
        config["missions"]
    )
    capsys.readouterr()


@pytest.mark.parametrize("supervised", [False, True])
def test_expectation_failure_exit_preserves_legacy_behavior(work_tmp, supervised):
    mission = _supervised_mission() if supervised else _mission()
    payload = {"defaults": {}, "missions": [mission]}
    suite_path = work_tmp / "suite.json"
    suite_path.write_text(json.dumps(payload), encoding="utf-8")

    def fake_runner(command, **kwargs):
        episode = {"world": mission["world"], "status": "complete"}
        Path(command[command.index("--report") + 1]).write_text(
            json.dumps({"episodes": [episode]}), encoding="utf-8"
        )

    # The historical default only treats a missing scored row as a failing
    # suite exit.  Failed expectations remain visible in the report but do
    # not change that default status.
    assert (
        suite.main(["--suite", str(suite_path), "--out", str(work_tmp / "out")], runner=fake_runner)
        == 0
    )
    table = json.loads((work_tmp / "out/suite-report.json").read_text("utf-8"))
    assert ("supervisor" in table["missions"][0]) == supervised
    assert ("Supervisor outcomes" in suite.render_markdown(table)) == supervised


def test_strict_exit_rejects_expectation_failure_and_collector_error(work_tmp):
    mission = _mission()
    suite_path = work_tmp / "suite.json"
    suite_path.write_text(json.dumps({"defaults": {}, "missions": [mission]}), encoding="utf-8")

    def fake_runner(command, **kwargs):
        raise subprocess.CalledProcessError(7, command)

    # Strict mode is opt-in for model-round runs.  It reports the collector
    # failure as nonzero while the default mode above remains legacy-compatible.
    assert (
        suite.main(
            ["--suite", str(suite_path), "--out", str(work_tmp / "strict"), "--strict"],
            runner=fake_runner,
        )
        == 1
    )


def test_build_command_strict_is_explicit():
    mission = _mission()
    base = suite.build_command(mission, Path("onboard.json"), Path("campaign"), Path("report.json"))
    strict = suite.build_command(
        mission, Path("onboard.json"), Path("campaign"), Path("report.json"), strict=True
    )
    assert "--strict" not in base
    assert strict[strict.index("--strict")] == "--strict"


def test_dry_run_rejects_invalid_package_before_writing_config(work_tmp):
    mission = _mission(mission_package={"mission_id": "bad"}, sim_ground_events=[])
    suite_path = work_tmp / "suite.json"
    suite_path.write_text(json.dumps({"defaults": {}, "missions": [mission]}), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        suite.main(["--suite", str(suite_path), "--out", str(work_tmp / "out"), "--dry-run"])
    assert exc.value.code == 2
    assert not (work_tmp / "out").exists()


def test_dry_run_rejects_invalid_sim_timeline(work_tmp):
    v2 = json.loads((suite.ROOT / "configs/onboard/mission-suite-v2.json").read_text("utf-8"))
    mission = v2["missions"][8]
    mission["sim_ground_events"] = [{"t_s": -1, "event": "abort"}]
    suite_path = work_tmp / "suite.json"
    suite_path.write_text(json.dumps({"defaults": {}, "missions": [mission]}), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        suite.main(["--suite", str(suite_path), "--out", str(work_tmp / "out"), "--dry-run"])
    assert exc.value.code == 2
    assert not (work_tmp / "out").exists()
