from __future__ import annotations

import runpy
from pathlib import Path

import pytest

from multidetect.vision import CaptureConfig

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/mine_camera_hard_negatives.py"


def _script_namespace() -> dict[str, object]:
    return runpy.run_path(str(SCRIPT))


def test_hard_negative_miner_reads_secret_rtsp_source_from_environment(
    monkeypatch,
) -> None:
    namespace = _script_namespace()
    parser = namespace["build_parser"]()
    secret = "rtsp://SECRET_USER:SECRET_PASSWORD@192.0.2.10/stream"
    monkeypatch.setenv("TEST_HARDNEG_CAMERA", secret)
    args = parser.parse_args(
        [
            "--onnx-model",
            "candidate.onnx",
            "--out",
            "capture-output",
            "--source-env",
            "TEST_HARDNEG_CAMERA",
            "--session-id",
            "bench-no-fire-001",
            "--confirm-no-fire",
        ]
    )

    source = namespace["_capture_source"](args)
    config = CaptureConfig(source=source)

    assert source == secret
    assert config.is_rtsp is True
    assert config.redacted_source_description == "RTSP source"
    assert secret not in config.redacted_source_description


def test_hard_negative_miner_requires_session_id_and_mutually_exclusive_source() -> None:
    parser = _script_namespace()["build_parser"]()

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--onnx-model",
                "candidate.onnx",
                "--out",
                "capture-output",
                "--confirm-no-fire",
            ]
        )
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--onnx-model",
                "candidate.onnx",
                "--out",
                "capture-output",
                "--source",
                "0",
                "--source-env",
                "CAMERA_SOURCE",
                "--session-id",
                "bench-no-fire-001",
                "--confirm-no-fire",
            ]
        )
