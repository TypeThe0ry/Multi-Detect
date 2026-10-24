from __future__ import annotations

import sys
import types

from scripts import run_operator_demo as demo
from scripts.run_nightly_regression import SitlLock


def sample_link():
    return {
        "operator_port": 14580,
        "operator_bind_host": "127.0.0.1",
        "operator_hmac_key_hex": "a" * 64,
        "operator_mavlink_signing_key_hex": "b" * 64,
        "operator_package_key_hex": "c" * 64,
    }


def test_gcs_environment_maps_session_keys_and_disables_automation():
    env = demo.gcs_environment(sample_link(), {"MULTIDETECT_UI_AUTO_EXIT_S": "2", "KEEP": "yes"})
    assert env["MULTIDETECT_OPERATOR_UDP_HOST"] == "127.0.0.1"
    assert env["MULTIDETECT_OPERATOR_UDP_PORT"] == "14580"
    assert env["MULTIDETECT_OPERATOR_UDP_LOCAL_PORT"] == "14581"
    assert env["MULTIDETECT_OPERATOR_KEY"] == "a" * 64
    assert env["MULTIDETECT_OPERATOR_MAVLINK_KEY_HEX"] == "b" * 64
    assert env["MULTIDETECT_OPERATOR_SIGNING_KEY"] == "hmac:" + "c" * 64
    assert env["MULTIDETECT_GROUND_COMMAND_KEY"] == "hmac:" + "a" * 64
    assert "MULTIDETECT_UI_AUTO_EXIT_S" not in env
    assert env["KEEP"] == "yes"


def test_cleanup_targets_only_recorded_resources(monkeypatch):
    calls = []

    class Process:
        def __init__(self, pid):
            self.pid = pid

        def poll(self):
            return None

        def terminate(self):
            calls.append(("terminate", self.pid))

        def wait(self, timeout):
            calls.append(("wait", self.pid, timeout))

    class FakePsutilProcess:
        def __init__(self, pid):
            self.pid = pid

        def children(self, recursive=False):
            return []

    monkeypatch.setitem(sys.modules, "psutil", types.SimpleNamespace(Process=FakePsutilProcess))
    monkeypatch.setattr(demo.subprocess, "run", lambda argv, **kw: calls.append(("docker", argv)))
    demo.cleanup(demo.OwnedResources(Process(42), Process(43), "demo-owned"), "docker-test")
    assert calls[:4] == [("terminate", 43), ("wait", 43, 8), ("terminate", 42), ("wait", 42, 8)]
    assert calls[4][1][-1] == "demo-owned"
    assert calls[5][1][-1] == "demo-owned"


def test_sitl_lock_release_preserves_foreign_owner(tmp_path):
    lock = SitlLock(tmp_path / "sitl.lock", label="demo", retry_s=0, stale_s=0)
    lock.acquire(wait=False)
    lock.owner.write_text("other 999999999\n", encoding="utf-8")
    lock.release()
    assert lock.path.exists()
    assert lock.owner.read_text(encoding="utf-8").startswith("other ")


def test_sitl_lock_acquire_and_release(tmp_path):
    lock = SitlLock(tmp_path / "sitl.lock", label="demo", retry_s=0)
    lock.acquire(wait=False)
    assert lock.path.is_dir()
    assert "demo " in lock.owner.read_text(encoding="utf-8")
    lock.release()
    assert not lock.path.exists()


def test_dry_run_prints_redacted_config_without_launching(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(demo, "SCRATCH", tmp_path / "scratch")

    def unexpected_process(*args, **kwargs):
        raise AssertionError("Popen called")

    monkeypatch.setattr(demo.subprocess, "Popen", unexpected_process)
    assert demo.main(["--dry-run", "--task-package", str(tmp_path / "task.json")]) == 0
    output = capsys.readouterr().out
    assert "SIM command:" in output
    assert "GCS environment:" in output
    assert "MULTIDETECT_OPERATOR_KEY" in output
    assert "0000...0000" in output
    assert "hmac:0000" not in output


def test_gcs_stream_geometry_matches_the_onboard_operator_link():
    # The GCS stream geometry must equal the onboard registration or selection
    # commands are rejected (scripts/onboard_main.py registers 1280x720).
    import inspect

    import scripts.onboard_main as onboard_main
    import scripts.run_operator_demo as demo

    assert 'VideoGeometry("camera-main", 1280, 720)' in inspect.getsource(onboard_main)
    source = inspect.getsource(demo)
    assert '"MULTIDETECT_OPERATOR_STREAM_WIDTH": "1280"' in source
    assert '"MULTIDETECT_OPERATOR_STREAM_HEIGHT": "720"' in source
