from __future__ import annotations

from pathlib import Path

import pytest

from scripts import bringup_orin


def test_parser_requires_host_and_user() -> None:
    parser = bringup_orin.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args([])
    with pytest.raises(SystemExit):
        parser.parse_args(["--host", "192.0.2.10"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--user", "jetson"])


def test_build_commands_use_key_only_ssh_and_expected_order(tmp_path: Path) -> None:
    commands = bringup_orin.build_commands(
        "192.0.2.10", "jetson", tmp_path / "id_ed25519", "20261020T010203Z"
    )

    assert len(commands) == 4
    assert commands[0][0] == "ssh"
    assert commands[1][0] == "scp"
    assert commands[2][0] == "ssh"
    assert commands[3][0] == "scp"
    for command in commands:
        assert "-i" in command
        assert "BatchMode=yes" in command
        assert "PasswordAuthentication=no" in command
        assert "KbdInteractiveAuthentication=no" in command
        assert "PreferredAuthentications=publickey" in command
    assert "--run-benchmark" in commands[2][-1]
    assert "sudo -n bash" in commands[2][-1]
    assert "artifacts/orin/20261020T010203Z/orin-v3.json" in commands[2][-1]
    assert commands[3][-1] == str(bringup_orin.ROOT / "artifacts/orin")


def test_dry_run_prints_commands_without_running_or_writing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    invoked: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> None:
        invoked.append(command)

    monkeypatch.setattr(bringup_orin.subprocess, "run", fake_run)
    assert bringup_orin.main(["--host", "192.0.2.10", "--user", "jetson", "--dry-run"]) == 0

    output = capsys.readouterr().out
    lines = [line for line in output.splitlines() if line]
    assert len(lines) == 4
    assert all(line.startswith(("ssh ", "scp ")) for line in lines)
    assert invoked == []

