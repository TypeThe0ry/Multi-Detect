import argparse
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from multidetect.clock import HighResolutionSystemClock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from scripts import run_vtol_mission_sensor_capture as outer  # noqa: E402
from scripts import vtol_sitl_mission_acceptance as mission  # noqa: E402


def options(**changes):
    values = dict(
        host_clock="perf_counter",
        host_clock_session_id="parent-run-a",
        acknowledge_owned_disposable_sitl=True,
        mission_active_ready_file=None,
        flight_capture_ready_file=None,
        flight_capture_complete_file=None,
    )
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    "changes",
    [
        {"host_clock_session_id": None},
        {"host_clock_session_id": " "},
        {"host_clock_session_id": "x" * 257},
        {"host_clock": "monotonic"},
        {"acknowledge_owned_disposable_sitl": False},
    ],
)
def test_cli_rejects_unbound_and_unmigrated_options(changes):
    with pytest.raises(SystemExit) as exc:
        mission._cli_host_clock(options(**changes), argparse.ArgumentParser())
    assert exc.value.code == 2


def test_child_clock_reconstructs_same_host_identity():
    parent = HighResolutionSystemClock(session_id="parent-run-a")
    child = mission._cli_host_clock(options(), argparse.ArgumentParser())
    assert child.identity() == parent.identity()
    before = parent.read()
    assert child.read().elapsed_since(before) >= 0


def test_legacy_cli_keeps_default_clock():
    assert (
        mission._cli_host_clock(
            options(host_clock="monotonic", host_clock_session_id=None), argparse.ArgumentParser()
        )
        is None
    )


def process_options(tmp_path):
    paths = SimpleNamespace(
        **{
            key: tmp_path / key
            for key in (
                "mission_evidence",
                "flight_ready",
                "flight_capture_complete",
                "mission_journal",
            )
        }
    )
    return dict(
        paths=paths,
        scenario=tmp_path / "scenario",
        container_name="owned",
        container_id="a" * 64,
        evaluator_port=14580,
        airframe_profile_sha256="b" * 64,
        runtime_descriptor_sha256="c" * 64,
        runtime_provenance_sha256="d" * 64,
        mission_timeout_s=600,
        docker_executable="docker",
        campaign_file=tmp_path / "campaign.json",
        host_clock_identity=HighResolutionSystemClock(session_id="parent-run-a").identity(),
    )


def test_parent_command_binds_child_and_omits_camera_barriers(tmp_path):
    args = outer._process_arguments(**process_options(tmp_path))
    assert args[args.index("--host-clock") + 1] == "perf_counter"
    assert args[args.index("--host-clock-session-id") + 1] == "parent-run-a"
    assert "--flight-capture-ready-file" not in args
    assert "--flight-capture-complete-file" not in args


def test_parent_command_binds_migrated_camera_path(tmp_path):
    args = process_options(tmp_path)
    args["campaign_file"] = None
    command = outer._process_arguments(**args)
    assert command[command.index("--host-clock-session-id") + 1] == "parent-run-a"
    assert "--flight-capture-ready-file" in command
    assert "--flight-capture-complete-file" in command
    child = mission._cli_host_clock(
        options(
            flight_capture_ready_file=Path("ready"), flight_capture_complete_file=Path("complete")
        ),
        argparse.ArgumentParser(),
    )
    assert child.identity() == args["host_clock_identity"]


@pytest.mark.parametrize("campaign,capture", [(Path("missing"), False)])
def test_outer_qpc_invalid_combinations_fail_before_files_or_processes(tmp_path, campaign, capture):
    with pytest.raises(outer.VtolMissionSensorCaptureError, match="QPC currently requires"):
        outer.run_vtol_mission_sensor_capture(
            tmp_path / "missing-scenario",
            tmp_path / "evidence.json",
            campaign_file=campaign,
            capture_clock=capture,
            host_clock="perf_counter",
        )
    assert not list(tmp_path.iterdir())


def test_mission_main_passes_clock_to_session_without_energy(monkeypatch):
    seen = {}

    class Session:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            seen["closed"] = True

    monkeypatch.setattr(mission, "MissionTelemetrySession", Session)
    monkeypatch.setattr(mission, "_run_cli", lambda args, **kwargs: 0)
    argv = [
        "--evidence",
        "unused",
        "--container-name",
        "owned",
        "--expected-container-id",
        "a" * 64,
        "--evaluator-port",
        "14580",
        "--airframe-profile-sha256",
        "b" * 64,
        "--runtime-descriptor-sha256",
        "c" * 64,
        "--runtime-provenance-sha256",
        "d" * 64,
        "--acknowledge-owned-disposable-sitl",
        "--host-clock",
        "perf_counter",
        "--host-clock-session-id",
        "parent-run-a",
    ]
    assert mission.main(argv) == 0
    assert seen["clock"].session_id == "parent-run-a"
    assert seen["energy_profile"] is None
    assert seen["closed"] is True
