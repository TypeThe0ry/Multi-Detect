"""Process-level REAL-mode loopback: onboard_main + fake PX4 + fake GCS."""

from __future__ import annotations

import json
import os
import subprocess
import sys


def test_real_mode_process_loopback(tmp_path):
    env = os.environ.copy()
    env.update(PYTHONPATH="src;.", PYTHONUTF8="1", CX_REAL_HARNESS_SCRATCH=str(tmp_path))
    result = subprocess.run(
        [sys.executable, "scripts/run_real_mode_harness.py"],
        env=env,
        cwd=os.getcwd(),
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["onboard_returncode"] == 0
    assert report["gcs_returncode"] in (0, 1, -15)
    assert report["final_log"]["kind"] == "final_summary"
    assert report["px4_commands"]
