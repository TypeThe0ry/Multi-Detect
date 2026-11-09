from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from multidetect.simulator.cpu import resolve_cpu_isolation
from scripts import collect_sim_teacher_episodes as collector
from scripts import run_mission_suite as suite
from scripts import run_r15_round as r15
from scripts.run_simulator_docker import DockerSimulatorPlanError, build_docker_simulator_plan

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "configs/scenarios/river_rescue_vtol_001.scenario.json"


@pytest.fixture(autouse=True)
def cpu_environment(monkeypatch):
    for key in (
        "MULTIDETECT_SIM_CPUSET", "MULTIDETECT_SIM_CPU_POOL",
        "MULTIDETECT_SIM_CPUS_PER_SLOT", "MULTIDETECT_SIM_CPU_SHARES",
        "MULTIDETECT_SITL_SLOT", "MULTIDETECT_SITL_SLOTS", "MULTIDETECT_SITL_LOCK_HELD",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("multidetect.simulator.cpu.os.cpu_count", lambda: 96)
    # Planning must never discover a bridge gateway by contacting Docker.
    monkeypatch.setenv("MULTIDETECT_SIM_LISTEN_HOST", "127.0.0.1")


def _auto(monkeypatch, *, pool="48-95", slots=4):
    monkeypatch.setenv("MULTIDETECT_SIM_CPUSET", "auto")
    monkeypatch.setenv("MULTIDETECT_SIM_CPU_POOL", pool)
    monkeypatch.setenv("MULTIDETECT_SITL_SLOTS", str(slots))


def _collection_plan(tmp_path):
    document = json.loads(SCENARIO.read_text(encoding="utf-8"))
    return collector.CollectionPlan(
        str(SCENARIO),
        str((SCENARIO.parent / document["channels"]["runtime_input"]["path"]).resolve()),
        str((SCENARIO.parent / document["channels"]["truth"]["path"]).resolve()),
        "hover", 1, 1, 0, str(tmp_path), min_free_gb=0,
    )


@pytest.mark.parametrize("selector", [None, "", "off", "false", "no"])
def test_disabled_cpu_environment_does_not_change_command(tmp_path, monkeypatch, selector):
    baseline = build_docker_simulator_plan(SCENARIO, job_dir=tmp_path / "baseline")
    if selector is not None:
        monkeypatch.setenv("MULTIDETECT_SIM_CPUSET", selector)
    # Unused settings must not make the default-off path fail or add flags.
    monkeypatch.setenv("MULTIDETECT_SIM_CPU_POOL", "bad")
    monkeypatch.setenv("MULTIDETECT_SIM_CPUS_PER_SLOT", "bad")
    monkeypatch.setenv("MULTIDETECT_SIM_CPU_SHARES", "bad")
    plan = build_docker_simulator_plan(SCENARIO, job_dir=tmp_path / "disabled")
    assert plan.command == baseline.command
    assert plan.command_text.encode() == baseline.command_text.encode()
    assert plan.cpu_isolation is None
    assert "cpu_isolation" not in plan.to_mapping()


def test_auto_assigns_disjoint_blocks_without_modulo_reuse(monkeypatch):
    _auto(monkeypatch, slots=8)
    allocations = [resolve_cpu_isolation(i) for i in range(8)]
    assert [a.cpuset_cpus for a in allocations] == [
        "48-53", "54-59", "60-65", "66-71", "72-77", "78-83", "84-89", "90-95",
    ]
    assert all(a.cpu_shares == 4096 and a.cpus_per_slot == 6 for a in allocations)
    with pytest.raises(ValueError, match="insufficient.*9 slots x 6"):
        resolve_cpu_isolation(8)


def test_auto_uses_all_local_cpus_by_default(monkeypatch):
    monkeypatch.setenv("MULTIDETECT_SIM_CPUSET", "auto")
    allocation = resolve_cpu_isolation(1)
    assert allocation.cpuset_cpus == "6-11"
    assert allocation.cpu_pool == "0-95"


def test_auto_sorts_sparse_pool_and_respects_custom_size_and_shares(monkeypatch):
    _auto(monkeypatch, pool="90-93,48,50,60-61", slots=2)
    monkeypatch.setenv("MULTIDETECT_SIM_CPUS_PER_SLOT", "4")
    monkeypatch.setenv("MULTIDETECT_SIM_CPU_SHARES", "8192")
    allocation = resolve_cpu_isolation(1)
    assert allocation.cpuset_cpus == "90-93"
    assert allocation.cpu_pool == "48,50,60-61,90-93"
    assert allocation.cpu_shares == 8192


def test_lease_overrides_planned_worker_and_preview_can_select_a_slot(monkeypatch):
    _auto(monkeypatch)
    monkeypatch.setenv("MULTIDETECT_SITL_SLOT", "2")
    monkeypatch.setenv("MULTIDETECT_SITL_LOCK_HELD", "1")
    assert resolve_cpu_isolation(0).cpuset_cpus == "60-65"
    assert resolve_cpu_isolation(0).slot_index == 2
    assert resolve_cpu_isolation(slot_index=1).cpuset_cpus == "54-59"


def test_explicit_list_and_cpu_zero_are_not_treated_as_off(monkeypatch):
    monkeypatch.setenv("MULTIDETECT_SIM_CPUSET", " 54,48-50 ")
    allocation = resolve_cpu_isolation()
    assert allocation.cpuset_cpus == "48-50,54"
    assert allocation.mode == "explicit"
    assert allocation.cpus_per_slot == 4
    monkeypatch.setenv("MULTIDETECT_SIM_CPUSET", "0")
    assert resolve_cpu_isolation().cpuset_cpus == "0"


@pytest.mark.parametrize(
    "pool", ["", "48,", "-1", "6-3", "0--3", "1.5", "0-2:1", "0-96", "0-999999999"]
)
def test_auto_rejects_invalid_cpu_ranges(monkeypatch, pool):
    _auto(monkeypatch, pool=pool)
    with pytest.raises(ValueError, match="MULTIDETECT_SIM_CPU_POOL"):
        resolve_cpu_isolation()


@pytest.mark.parametrize("pool", ["48-54,54-60", "48,48", "48-60,50-52"])
def test_auto_rejects_overlap_instead_of_deduplicating(monkeypatch, pool):
    _auto(monkeypatch, pool=pool)
    with pytest.raises(ValueError, match="overlapping ranges/duplicate CPU"):
        resolve_cpu_isolation()


@pytest.mark.parametrize(
    "selector,label", [("auto", "MULTIDETECT_SIM_CPU_POOL"), ("48-50,50", "MULTIDETECT_SIM_CPUSET")]
)
def test_explicit_and_auto_validation_fail_before_job_directory_creation(
    tmp_path, monkeypatch, selector, label
):
    monkeypatch.setenv("MULTIDETECT_SIM_CPUSET", selector)
    monkeypatch.setenv("MULTIDETECT_SIM_CPU_POOL", "48-50,50")
    with pytest.raises(DockerSimulatorPlanError, match=label):
        build_docker_simulator_plan(SCENARIO, job_dir=tmp_path / "no-job")
    assert not (tmp_path / "no-job").exists()


def test_even_slot_zero_requires_capacity_for_all_configured_slots(monkeypatch):
    _auto(monkeypatch, pool="48-59", slots=3)
    with pytest.raises(ValueError, match="12 available; 3 slots x 6 CPUs require 18"):
        resolve_cpu_isolation(0)


def test_explicit_list_refuses_multi_slot_overlap_and_out_of_pool(monkeypatch):
    _auto(monkeypatch)
    monkeypatch.setenv("MULTIDETECT_SIM_CPUSET", "48-53")
    with pytest.raises(ValueError, match="overlap across SITL slots"):
        resolve_cpu_isolation()
    monkeypatch.setenv("MULTIDETECT_SITL_SLOTS", "1")
    monkeypatch.setenv("MULTIDETECT_SIM_CPU_POOL", "54-59")
    with pytest.raises(ValueError, match="outside MULTIDETECT_SIM_CPU_POOL"):
        resolve_cpu_isolation()


@pytest.mark.parametrize("variable,value", [
    ("MULTIDETECT_SIM_CPUS_PER_SLOT", "0"), ("MULTIDETECT_SIM_CPUS_PER_SLOT", "-2"),
    ("MULTIDETECT_SIM_CPUS_PER_SLOT", "1.5"), ("MULTIDETECT_SIM_CPU_SHARES", "1"),
    ("MULTIDETECT_SIM_CPU_SHARES", "262145"), ("MULTIDETECT_SIM_CPU_SHARES", "oops"),
    ("MULTIDETECT_SITL_SLOTS", "0"), ("MULTIDETECT_SITL_SLOTS", "many"),
    ("MULTIDETECT_SITL_SLOT", "-1"), ("MULTIDETECT_SITL_SLOT", "4"),
])
def test_invalid_sizes_shares_and_leases_are_clear_errors(monkeypatch, variable, value):
    _auto(monkeypatch)
    monkeypatch.setenv(variable, value)
    with pytest.raises(ValueError, match=variable):
        resolve_cpu_isolation()


@pytest.mark.parametrize("network", ["bridge", "host"])
def test_cpu_flags_coexist_with_gpu_network_and_hash_bound_plan(tmp_path, monkeypatch, network):
    if network == "host" and not sys.platform.startswith("linux"):
        pytest.skip("host networking is Linux-only")
    kwargs = dict(
        job_dir=tmp_path, gpu_render=True, gpu_device=0, sim_network=network, sim_instance=2
    )
    baseline = build_docker_simulator_plan(SCENARIO, **kwargs)
    _auto(monkeypatch)
    monkeypatch.setenv("MULTIDETECT_SITL_SLOT", "2")
    plan = build_docker_simulator_plan(SCENARIO, **kwargs)
    index = plan.command.index("--cpuset-cpus")
    assert plan.command[index : index + 4] == ("--cpuset-cpus", "60-65", "--cpu-shares", "4096")
    assert plan.command[:index] + plan.command[index + 4 :] == baseline.command
    assert "--cpus" not in plan.command and "--cpu-quota" not in plan.command
    assert plan.to_mapping()["cpu_isolation"] == plan.cpu_isolation.to_mapping()
    assert plan.to_mapping()["cpu_isolation"]["slot_index"] == 2
    assert plan.evidence_hash != baseline.evidence_hash


def test_explicit_cpu_command_uses_custom_shares_and_is_serialized(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTIDETECT_SIM_CPUSET", "48-50,54")
    monkeypatch.setenv("MULTIDETECT_SIM_CPU_SHARES", "8192")
    plan = build_docker_simulator_plan(SCENARIO, job_dir=tmp_path / "job")
    assert plan.command[plan.command.index("--cpuset-cpus") + 1] == "48-50,54"
    assert plan.command[plan.command.index("--cpu-shares") + 1] == "8192"
    path = plan.write_json(tmp_path / "plan.json")
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["cpu_isolation"]["cpuset_cpus"] == "48-50,54"
    assert document["cpu_isolation"]["mode"] == "explicit"
    assert document["sha256"] == plan.evidence_hash


def test_collection_run_plan_uses_leased_slot_not_initial_worker(tmp_path, monkeypatch):
    _auto(monkeypatch)
    monkeypatch.setenv("MULTIDETECT_SITL_SLOT", "2")
    plan = _collection_plan(tmp_path)
    result = plan.to_mapping()
    command = result["docker_command"]
    assert result["worker_index"] == 0
    assert result["cpu_isolation"]["slot_index"] == 2
    assert command[command.index("--cpuset-cpus") + 1] == "60-65"
    assert result["cpu_isolation"]["cpuset_cpus"] == "60-65"


def test_standalone_collector_resolves_cpu_after_acquiring_its_own_lease(tmp_path, monkeypatch):
    _auto(monkeypatch)
    monkeypatch.setattr(collector, "_docker_available", lambda: True)
    monkeypatch.setattr(collector, "_validate_spawn_clearance", lambda _: None)
    monkeypatch.setattr(collector, "SitlLock", lambda *_a, **_kw: SimpleNamespace(
        slot=2, acquire=lambda: None, release=lambda: None,
    ))
    allocations = []

    def record_episode(plan, *_):
        allocation = plan.to_mapping()["cpu_isolation"]
        allocations.append(allocation)
        episode = tmp_path / "recorded"
        episode.mkdir()
        (episode / "episode.json").write_text('{"status": "complete"}', encoding="utf-8")
        return episode

    monkeypatch.setattr(collector, "run_episode", record_episode)
    assert collector.main([
        "--scenario", str(SCENARIO), "--airframe", "vtol", "--teacher", "hover",
        "--episodes", "1", "--steps-per-episode", "1",
        "--out", str(tmp_path), "--min-free-gb", "0",
    ]) == 0
    assert len(allocations) == 1
    assert allocations[0]["slot_index"] == 2
    assert allocations[0]["cpuset_cpus"] == "60-65"


def test_mission_suite_dry_run_exposes_cpu_allocation(tmp_path, monkeypatch, capsys):
    _auto(monkeypatch)
    assert suite.main([
        "--out", str(tmp_path), "--only", "count-cones", "--dry-run",
    ]) == 0
    row = json.loads(capsys.readouterr().out)
    slot = int(row["command"][row["command"].index("--worker-index") + 1])
    assert row["cpu_isolation"] == resolve_cpu_isolation(slot).to_mapping()


def test_mission_suite_passes_actual_global_lease_to_child(tmp_path, monkeypatch):
    _auto(monkeypatch)
    leases = []
    allocations = []

    class Lease:
        slot = 2

        def __init__(self, *_a, **kwargs):
            assert kwargs["slots"] == 4
            leases.append(self)

        def acquire(self):
            pass

        def release(self):
            self.released = True

    def fake_run(command, *, env, **_kwargs):
        assert command[command.index("--worker-index") + 1] == "2"
        assert env["MULTIDETECT_SITL_SLOT"] == "2"
        with monkeypatch.context() as child:
            child.setenv("MULTIDETECT_SITL_SLOT", env["MULTIDETECT_SITL_SLOT"])
            allocations.append(resolve_cpu_isolation(0))
        report = Path(command[command.index("--report") + 1])
        report.write_text('{"episodes": []}', encoding="utf-8")

    monkeypatch.setattr(suite, "SitlLock", Lease)
    monkeypatch.setattr(suite.subprocess, "run", fake_run)
    monkeypatch.setattr(suite, "build_provenance", lambda **_: {})
    suite.main(["--out", str(tmp_path), "--only", "count-cones"])
    assert len(leases) == len(allocations) == 1
    assert leases[0].released
    assert allocations[0].cpuset_cpus == "60-65"


@pytest.mark.parametrize("attach", [False, True])
def test_episode_provenance_records_affinity_and_does_not_claim_attached_affinity(
    tmp_path, monkeypatch, attach
):
    _auto(monkeypatch)
    monkeypatch.setenv("MULTIDETECT_PX4_LOGGER_MODE", "off")
    monkeypatch.setenv("LINUX_PRIMITIVE_CLOCK_STALL_TIMEOUT_S", "90")
    monkeypatch.setenv("MULTIDETECT_SITL_SLOT", "2")
    plan = _collection_plan(tmp_path)
    if attach:
        plan = replace(plan, attach_container="existing-container")
    monkeypatch.setattr(collector, "_hover_policy", lambda *_: object())
    monkeypatch.setattr(collector, "build_provenance", lambda **_: {})
    monkeypatch.setattr(collector, "snapshot_config", lambda *_: None)
    monkeypatch.setattr(
        collector.subprocess,
        "run",
        lambda command, **kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="fake Docker unavailable"
        ),
    )

    def stop_before_docker(_self):
        raise RuntimeError("offline stop before Docker")

    monkeypatch.setattr(collector.DockerSimEnvironment, "__enter__", stop_before_docker)
    with pytest.raises(RuntimeError, match="offline stop"):
        collector.run_episode(plan, Path(plan.truth), 0)
    meta_path, = (tmp_path / "episodes").glob("*/episode.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["provenance"]["px4_logger_mode"] == "off"
    assert meta["provenance"]["px4_daemon_mode"] is True
    assert meta["provenance"]["linux_primitive_clock_stall_timeout_s"] == 90.0
    allocation = meta["provenance"]["simulator_cpu_isolation"]
    if attach:
        assert allocation == {"mode": "attached", "cpuset_cpus": None, "cpu_shares": None}
    else:
        assert allocation["cpuset_cpus"] == "60-65"
        assert allocation["slot_index"] == 2
        assert allocation["cpu_shares"] == 4096


def test_mission_suite_rejects_insufficient_global_pool_before_writing(tmp_path, monkeypatch):
    _auto(monkeypatch, pool="48-53", slots=2)
    with pytest.raises(SystemExit, match="2"):
        suite.main(["--out", str(tmp_path / "no-output"), "--dry-run"])
    assert not (tmp_path / "no-output").exists()


def test_r15_rejects_cpu_pool_before_preflight_or_workers(tmp_path, monkeypatch):
    _auto(monkeypatch, pool="48-53", slots=2)
    runner = r15.Round(SimpleNamespace(out=tmp_path / "round"))
    with pytest.raises(ValueError, match="insufficient"):
        runner.collect()
    assert not runner.out.exists()


def test_r15_runtime_lease_uses_global_slots_and_rewrites_worker(tmp_path, monkeypatch):
    _auto(monkeypatch)
    runner = r15.Round(SimpleNamespace(
        out=tmp_path / "round", scenarios=SCENARIO.parent, r15_index=Path("unused"),
        episodes=tmp_path / "episodes", workers=1, steps_per_episode=1, teacher_target="true",
        retries=1, dry_run=False, min_free_memory_gb=0, episodes_per_world=1,
    ))
    monkeypatch.setattr(r15, "round_world_ids", lambda _: ["river_rescue_vtol_001"])
    monkeypatch.setattr(runner, "_collection_airframe", lambda _: ("vtol", None, {}))
    monkeypatch.setattr(runner, "collector_min_free_gb", lambda: 0)
    monkeypatch.setattr(runner, "stage_disk_need_gib", lambda _: 0)
    monkeypatch.setattr(runner, "run", lambda *_a, **_kw: None)
    commands = []

    class Lease:
        slot = 2

        def __init__(self, *args, **kwargs):
            assert kwargs["slots"] == 4  # one local worker may lease global slot 2

        def acquire(self):
            pass

        def release(self):
            pass

    def collect_one(_key, command, _target, *, env):
        commands.append(command)
        assert command[command.index("--worker-index") + 1] == "2"
        assert env["MULTIDETECT_SITL_SLOT"] == "2"
        assert env["MULTIDETECT_SITL_LOCK_HELD"] == "1"
        # Model the environment inherited by the collector child.
        with monkeypatch.context() as child:
            child.setenv("MULTIDETECT_SITL_SLOT", env["MULTIDETECT_SITL_SLOT"])
            assert resolve_cpu_isolation(0).cpuset_cpus == "60-65"
        return {"status": "complete", "attempts": 1}

    monkeypatch.setattr(r15, "SitlLock", Lease)
    monkeypatch.setattr(runner, "_collect_one", collect_one)
    runner.collect()
    assert len(commands) == 1
