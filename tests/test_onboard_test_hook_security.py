from __future__ import annotations

import os
import subprocess
import sys


ROOT = os.path.dirname(os.path.dirname(__file__))


def _run_with_hook(value: str, endpoint: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(PYTHONPATH="src;.", PYTHONUTF8="1")
    env["MULTIDETECT_ONBOARD_TEST_HOOK"] = value
    return subprocess.run(
        [
            sys.executable,
            "scripts/onboard_main.py",
            "real",
            "--endpoint",
            endpoint,
            "--failsafe",
            "land",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_non_whitelisted_hook_module_is_rejected_before_import():
    result = _run_with_hook("json", "udp:127.0.0.1:14550")
    assert result.returncode != 0
    assert "拒绝启用 onboard 测试 hook" in result.stdout + result.stderr


def test_whitelisted_hook_is_rejected_for_non_loopback_endpoint():
    result = _run_with_hook("scripts.onboard_real_test_hook", "udp:192.168.1.20:14550")
    assert result.returncode != 0
    assert "只允许 127.0.0.1" in result.stdout + result.stderr


def test_loopback_endpoint_forms_are_accepted():
    from scripts.onboard_main import _is_loopback_test_endpoint

    assert _is_loopback_test_endpoint("udp:127.0.0.1:14550")
    assert _is_loopback_test_endpoint("udpin:127.0.0.1:14550")
    assert _is_loopback_test_endpoint("tcp://localhost:14550")
    assert _is_loopback_test_endpoint("udp:[::1]:14550")
    assert not _is_loopback_test_endpoint("/dev/ttyTHS1")
    assert not _is_loopback_test_endpoint("udp:10.0.0.2:14550")


def test_no_hook_environment_does_not_activate_hook():
    env = os.environ.copy()
    env.pop("MULTIDETECT_ONBOARD_TEST_HOOK", None)
    env.update(PYTHONPATH="src;.", PYTHONUTF8="1")
    result = subprocess.run(
        [sys.executable, "scripts/onboard_main.py", "real", "--failsafe", "land"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert "测试 hook" not in result.stdout + result.stderr

