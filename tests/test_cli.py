from __future__ import annotations

import json
from pathlib import Path

from multidetect.cli import main

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs/missions/fire_suppression.demo.json"
REPLAY = ROOT / "examples/fire_mission_replay.jsonl"


def parsed_stdout(capsys) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def test_validate_config_command(capsys) -> None:
    assert main(["validate-config", str(CONFIG)]) == 0

    output = parsed_stdout(capsys)
    assert output[-1]["event"] == "config_valid"
    assert output[-1]["human_authorization_required"] is True


def test_replay_stops_at_redacted_authorization(capsys) -> None:
    assert main(["replay", str(CONFIG), str(REPLAY)]) == 0

    output = parsed_stdout(capsys)
    challenge = next(item for item in output if item["event"] == "authorization_required")
    finished = output[-1]
    assert challenge["nonce_redacted"] is True
    assert "nonce" not in challenge
    assert finished["pending_authorization"] is True
    assert finished["fake_release_request_count"] == 0


def test_explicit_simulation_cycle_writes_audit(tmp_path: Path, capsys) -> None:
    audit_path = tmp_path / "audit.jsonl"
    assert (
        main(
            [
                "replay",
                str(CONFIG),
                str(REPLAY),
                "--simulate-authorized-cycle",
                "--audit-out",
                str(audit_path),
            ]
        )
        == 0
    )

    output = parsed_stdout(capsys)
    finished = output[-1]
    audit_records = [
        json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()
    ]
    assert finished["simulated_cycle_completed"] is True
    assert finished["fake_release_request_count"] == 1
    assert any(record["event_type"] == "payload.release_confirmed" for record in audit_records)
