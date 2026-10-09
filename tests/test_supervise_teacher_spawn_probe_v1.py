"""Verify owned spawn-probe wiring, not an external execution result."""

import hashlib
import json
from pathlib import Path

import pytest

from multidetect.autonomy.serialization import canonical_sha256
from multidetect.simulation.manifest import load_scenario_manifest
from scripts import supervise_teacher_spawn_probe_v1 as probe

ROOT = Path(__file__).resolve().parents[1]


def test_candidate_changes_only_initial_spawn_and_binds_new_runtime():
    old = json.loads(
        (ROOT / "configs/scenarios/urban_inspection_multirotor_001.runtime.json").read_bytes()
    )
    new = json.loads((ROOT / probe.RUNTIME).read_bytes())
    pose = new["vehicle"].pop("spawn_pose")
    assert new == old
    assert pose == {
        "x_east_m": 0.0,
        "y_north_m": 0.0,
        "z_up_m": 0.15,
        "roll_rad": 0.0,
        "pitch_rad": 0.0,
        "yaw_rad": 0.0,
    }
    old_manifest = json.loads(
        (ROOT / "configs/scenarios/urban_inspection_multirotor_001.scenario.json").read_bytes()
    )
    new_manifest = json.loads((ROOT / probe.SCENARIO).read_bytes())
    assert new_manifest["channels"]["truth"] == old_manifest["channels"]["truth"]
    assert new_manifest["channels"]["expectations"] == old_manifest["channels"]["expectations"]
    assert new_manifest["channels"]["runtime_input"]["sha256"] == canonical_sha256(
        json.loads((ROOT / probe.RUNTIME).read_bytes())
    )
    # Exercise the actual loader, which hashes canonical channel JSON rather
    # than raw file whitespace. The first real preflight exposed this mismatch.
    assert (
        load_scenario_manifest(ROOT / probe.SCENARIO).runtime_input.vehicle.spawn_pose.z_up_m
        == 0.15
    )


@pytest.mark.parametrize("duration", [True, 0, 601, float("nan")])
def test_invalid_watchdog_budget_refused_before_launch(tmp_path, duration):
    with pytest.raises(ValueError):
        probe.supervise(tmp_path / "a", tmp_path / "b", timeout_s=duration)


def test_watchdog_contains_exact_scenario_and_no_flight_commands(monkeypatch, tmp_path):
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    directory = tmp_path / "artifacts/diagnostics/supervisor"
    diagnostics = tmp_path / "artifacts/diagnostics/worker"
    directory.parent.mkdir(parents=True)
    watchdog_sha = "f6672304cc822ffbf136ac6b271e73ed47d0621455824e7b6b40b6d997dfce69"

    def record(path, **kwargs):
        if Path(path).is_file():
            raw = Path(path).read_bytes()
            return {
                "path": Path(path).as_posix(),
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        return {"path": Path(path).as_posix(), "bytes": 29530, "sha256": watchdog_sha}

    commands = []

    def run(command, output, name, deadline):
        commands.append(command)
        diagnostics.mkdir()
        (diagnostics / "origin-window.json").write_text(
            json.dumps(
                {
                    "contract": "multidetect.teacher-SIM-origin-window-v1",
                    "status": "origin-window-recorded",
                }
            )
        )
        return {"native_exit_code": 0, "owned_tree_empty": True, "watchdog_fired": False}

    monkeypatch.setattr(probe.owned, "record", record)
    monkeypatch.setattr(probe.owned, "run_child", run)
    monkeypatch.setattr(probe.owned, "child_complete", lambda child: True)
    assert probe.supervise(directory, diagnostics) == 0
    command = commands[0]
    assert command[command.index("--scenario") + 1] == str(tmp_path / probe.SCENARIO)
    assert "--execute" in command
    assert "--force-arm" not in command
    report = json.loads((directory / "supervised-run.json").read_bytes())
    assert set(report["claims"].values()) == {False}
