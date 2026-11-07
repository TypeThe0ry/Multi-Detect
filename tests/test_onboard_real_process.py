"""Process-level REAL-mode loopback: onboard_main + fake PX4 + fake GCS."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

# Three real processes on wall-clock timeouts: on a loaded host (SITL plus little
# free memory) the onboard loop can stall past the camera watchdog.  Run it with
# `run_onboard_gate.py --with-timing` on a quiet machine.
pytestmark = pytest.mark.timing


def test_real_mode_process_loopback(tmp_path, require_loopback_sockets):
    env = os.environ.copy()
    env.update(
        PYTHONPATH=os.pathsep.join(("src", ".")),
        PYTHONUTF8="1",
        CX_REAL_HARNESS_SCRATCH=str(tmp_path),
    )
    result = None
    for _attempt in range(3):
        result = subprocess.run(
            [sys.executable, "scripts/run_real_mode_harness.py"],
            env=env,
            cwd=os.getcwd(),
            capture_output=True,
            text=True,
            timeout=110,
            check=False,
        )
        if result.returncode == 0 or "WinError 10013" not in result.stdout:
            break
    assert result is not None
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["onboard_returncode"] == 0
    assert report["gcs_returncode"] in (0, 1, -15)
    assert report["final_log"]["kind"] == "final_summary"
    assert report["final_log"]["summary"]["test_hook_active"] is True
    assert "离线真实模式测试 hook" in report["final_log"]["summary"]["test_hook_message_zh"]
    assert report["announced_plan"]["test_hook_active"] is True
    assert report["px4_commands"]
    assert "警告：已启用离线真实模式测试 hook" in (
        (tmp_path / "real-process-onboard.log").read_text(encoding="utf-8")
    )
