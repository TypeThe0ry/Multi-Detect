"""Import and argument smoke checks for mover-world verification."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from multidetect.sim_teacher import HoverPolicy
from multidetect.sim_teacher.primitives import ActionPrimitive
from scripts import collect_sim_teacher_episodes as collector
from scripts.verify_mover_world import build_collector_command

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "configs/scenarios/generated-movers/gen_mov_w0000.scenario.json"
TRUTH = SCENARIO.with_name("gen_mov_w0000.truth.json")


def test_collector_cli_imports_without_docker_and_exposes_verification_args() -> None:
    run = subprocess.run(
        [sys.executable, "scripts/collect_sim_teacher_episodes.py", "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert run.returncode == 0, run.stderr
    assert "hover" in run.stdout
    assert "--container-prefix" in run.stdout


def test_hover_teacher_starts_above_mover_trajectory_origin() -> None:
    # _hover_policy is the same collector factory used by the episode path;
    # it reads scenario truth and does not create a PX4 connection.
    policy = collector._hover_policy(None, TRUTH)

    assert isinstance(policy, HoverPolicy)
    action = policy.next_action(pose_ned=(0.0, 0.0, -8.0), yaw_deg=90.0)
    assert isinstance(action, ActionPrimitive)
    assert action.kind == "translate"
    target = policy.target_ned
    assert target[:2] == (22.0, -24.0)
    assert target[2] < 0  # NED down is negative while hovering above the mover.
    assert action.forward_m == pytest.approx(-18.0)
    assert action.right_m == pytest.approx(-12.4)
    turn = policy.next_action(pose_ned=(0.0, 0.0, -8.0), yaw_deg=90.0)
    assert turn.kind == "turn"


def test_verifier_collector_command_has_hover_segmentation_and_scoped_prefix() -> None:
    command = build_collector_command(
        "gen_mov_w0000", SCENARIO, ROOT / "artifacts/test-smoke", 30.0, 1.0
    )

    assert command[command.index("--teacher") + 1] == "hover"
    assert "--segmentation" in command
    assert command[command.index("--container-prefix") + 1] == "codex-movers"
    assert command[command.index("--control-port") + 1] == "14600"
    assert command[command.index("--telemetry-port") + 1] == "14601"
