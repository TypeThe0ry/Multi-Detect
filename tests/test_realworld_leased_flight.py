from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.realworld import leased_flight


@pytest.mark.parametrize(
    ("slot", "airframe", "world"),
    (
        (6, "standard_vtol_mono_cam", "rw_farm_iowa_w2km__clear_noon"),
        (5, "standard_vtol_mono_cam", "rw_farm_iowa_w6km__clear_noon"),
        (7, "rc_cessna_mono_cam", "rw_farm_iowa_w6km__clear_noon"),
    ),
)
def test_plan_only_builds_collector_command_without_live_work(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, slot: int, airframe: str, world: str
) -> None:
    calls = []

    def forbidden(*args, **kwargs):
        pytest.fail("plan-only must not rent a slot or start a rotor sampling thread")

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=9)

    monkeypatch.setenv("MULTIDETECT_SITL_SLOTS", "8")
    monkeypatch.setattr(leased_flight.Path, "is_file", lambda _path: True)
    monkeypatch.setattr(leased_flight, "SitlLock", forbidden)
    monkeypatch.setattr(leased_flight, "sitl_lock_context", forbidden)
    monkeypatch.setattr(leased_flight.threading, "Thread", forbidden)
    monkeypatch.setattr(leased_flight, "sample_rotors", forbidden)
    monkeypatch.setattr(leased_flight.subprocess, "run", run)
    out = tmp_path / "plan"

    assert leased_flight.main([str(slot), airframe, world, str(out), "--plan-only"]) == 9
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command == [
        leased_flight.SITL_PYTHON,
        "scripts/collect_sim_teacher_episodes.py",
        "--airframe", airframe,
        "--scenario", f"configs/scenarios/realworld/{airframe}/{world}.scenario.json",
        "--out", str(out),
        "--container-prefix", f"md-smoke-rw-slot-{slot}",
        "--control-port", str(14560 + 10 * slot),
        "--telemetry-port", str(14561 + 10 * slot),
        "--teacher", "explore", "--episodes", "1", "--steps-per-episode", "8",
        "--smoke-flight", "--dry-run",
    ]
    assert kwargs["cwd"] == leased_flight.ROOT
    assert kwargs["env"]["MULTIDETECT_SITL_SLOT"] == str(slot)
    assert not out.exists()


@pytest.mark.parametrize(("slots", "slot"), (("7", 5), ("8", 4), ("8", 8)))
def test_plan_only_rejects_slot_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, slots: str, slot: int
) -> None:
    monkeypatch.setenv("MULTIDETECT_SITL_SLOTS", slots)
    with pytest.raises(SystemExit, match="requires MULTIDETECT_SITL_SLOTS|only slots"):
        leased_flight.main([str(slot), "standard_vtol_mono_cam", "world", str(tmp_path), "--plan-only"])


def test_plan_only_rejects_extra_flight_arguments(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="2"):
        leased_flight.main(["5", "standard_vtol_mono_cam", "world", str(tmp_path), "--plan-only", "--timeout", "900"])


def test_plan_only_requires_sitl_python(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MULTIDETECT_SITL_SLOTS", "8")
    monkeypatch.setattr(leased_flight.Path, "is_file", lambda _path: False)
    with pytest.raises(SystemExit, match="missing SITL Python environment"):
        leased_flight.main(["5", "standard_vtol_mono_cam", "world", str(tmp_path), "--plan-only"])


def test_flight_command_preserves_explicit_scenario_and_extra(tmp_path: Path) -> None:
    scenario = "custom/place.scenario.json"
    extra = ["--timeout", "900"]
    command = leased_flight.flight_command(5, "x500_mono_cam", scenario, tmp_path, plan_only=False, extra=extra)
    assert command[:2] == [leased_flight.SITL_PYTHON, "scripts/smoke_airframe_flight.py"]
    assert command[command.index("--scenario") + 1] == scenario
    assert command[-2:] == extra
