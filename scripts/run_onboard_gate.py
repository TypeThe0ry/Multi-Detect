#!/usr/bin/env python3
"""Fixed onboard regression gate: run before every merge.

Runs the deterministic onboard autonomy / mission-planning test set (file list in
tests/onboard_gate_files.py) on the CPU only. Tests marked ``timing`` use real threads
and wall-clock deadlines, so their result depends on host load; they are skipped unless
``--with-timing`` is given (run that on a quiet machine). Extra arguments go to pytest.

    python scripts/run_onboard_gate.py -q -p no:warnings
    python scripts/run_onboard_gate.py --with-timing -q
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str]) -> int:
    with_timing = "--with-timing" in argv
    pytest_args = [arg for arg in argv if arg != "--with-timing"]
    sys.path.insert(0, str(ROOT / "tests"))
    from onboard_gate_files import validate

    files = [str(path) for path in validate()]
    if not files:
        print("onboard gate: missing or empty file list", file=sys.stderr)
        return 2
    base_temp = ROOT / ".pytest-basetemp" / "onboard"
    base_temp.mkdir(parents=True, exist_ok=True)
    # CPU-only so the gate never competes with a training or eval job for the GPU.
    env = {**os.environ, "CUDA_VISIBLE_DEVICES": "-1"}
    marker = "onboard" if with_timing else "onboard and not timing"
    started = time.monotonic()
    status = subprocess.call(
        [sys.executable, "-m", "pytest", "-m", marker, "--basetemp", str(base_temp),
         *files, *pytest_args],
        cwd=ROOT, env=env,
    )
    label = "PASS" if status == 0 else "FAIL"
    scope = " incl. timing" if with_timing else ""
    print(f"onboard gate{scope}: {label} ({time.monotonic() - started:.0f}s)")
    return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
