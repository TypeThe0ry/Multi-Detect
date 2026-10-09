from __future__ import annotations

import json

from scripts import probe_gazebo_capture_runtime as runtime


def test_missing_tools_do_not_claim_capture(monkeypatch):
    monkeypatch.setattr(runtime.shutil, "which", lambda _name: None)
    result = runtime.probe()
    assert result["docker_server"] is None
    assert result["container_capture_prerequisites_ready"] is False
    assert result["sensor_capture_performed"] is False
    assert result["training_ready"] is False


def test_exact_cached_image_is_only_a_prerequisite_not_rendering_proof(monkeypatch):
    monkeypatch.setattr(
        runtime.shutil, "which", lambda name: "docker" if name == "docker" else None
    )
    monkeypatch.setattr(
        runtime,
        "command_result",
        lambda command, _timeout: {
            "succeeded": True,
            "stdout": "27.0" if "info" in command else json.dumps([runtime.PINNED_IMAGE]),
        },
    )
    result = runtime.probe()
    assert result["container_capture_prerequisites_ready"] is True
    assert result["rendering_verified"] is False
    assert result["training_ready"] is False


def test_inspection_with_wrong_digest_is_not_ready(monkeypatch):
    monkeypatch.setattr(
        runtime.shutil, "which", lambda name: "docker" if name == "docker" else None
    )
    monkeypatch.setattr(
        runtime,
        "command_result",
        lambda _command, _timeout: {
            "succeeded": True,
            "stdout": json.dumps(["another/image@sha256:" + "0" * 64]),
        },
    )
    assert runtime.probe()["container_capture_prerequisites_ready"] is False
