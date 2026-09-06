from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_replay_rtsp_multiclient.py"
SCHEMA = ROOT / "configs" / "schemas" / "replay-rtsp-multiclient-bench.schema.json"


def test_multiclient_bench_uses_raw_loopback_transport_and_fail_closed_labels() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "class RawRtspClient" in source
    assert '"client_transport": "rtsp_interleaved_tcp_raw"' in source
    assert '"source_kind": "software_only_local_rtsp"' in source
    assert '"hardware_claim": "none"' in source
    assert '"release_impact": "does_not_close_hil_or_real_release_gates"' in source
    assert "minimum-client-count" in source
    assert "shared media fan-out" in source
    assert "shared media for this multi-client" in source
    assert '"shared_media": args.clients > 1' in source
    assert "warmup_frame" in source
    assert "rtp_packets_delta" in source
    assert "rtsp_interleaved_tcp_raw" not in source.split("def _parser", 1)[1]


def test_multiclient_bench_schema_accepts_latest_evidence() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    artifact_path = (
        ROOT
        / "artifacts"
        / "evaluation"
        / "replay-rtsp-multiclient-20260903-pre.8-v9-portable-4clients-shared.json"
    )
    if not artifact_path.is_file():
        pytest.skip("local RTSP multi-client evidence is not stored in Git")
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))

    errors = sorted(
        Draft202012Validator(schema).iter_errors(artifact),
        key=lambda error: list(error.path),
    )
    assert errors == []
    assert artifact["gate_passed"] is True
    assert artifact["hardware_claim"] == "none"
    assert artifact["clients_requested"] == 4
    assert artifact["frames_requested"] == 2
    assert artifact["shared_media"] is True
    assert artifact["helper_sha256"] == (
        "de94d5404bef48b80919bfc06465bd53ebfd50a8b65a50f38da3bcddadb58312"
    )
