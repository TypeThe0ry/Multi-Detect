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
    bundle = tmp_path / "bundle"
    steps = bringup_orin.build_commands(
        "192.0.2.10", "jetson", tmp_path / "id_ed25519", "20261020T010203Z", bundle
    )

    assert len(steps) == 3
    copy, install, fetch = steps
    # The prepared bundle is streamed whole; the repository's artifacts link never is.
    assert copy[0] == ["tar", "-C", str(bundle), "-cf", "-", "."]
    assert copy[1][0] == "ssh" and "tar -xf - -C /home/jetson/Multi-Detect" in copy[1][-1]
    assert install[0][0] == "ssh" and fetch[0][0] == "scp" and "-r" in fetch[0]
    for command in (copy[1], install[0], fetch[0]):
        assert "-i" in command
        for option in (
            "BatchMode=yes",
            "PasswordAuthentication=no",
            "KbdInteractiveAuthentication=no",
            "PreferredAuthentications=publickey",
            "StrictHostKeyChecking=accept-new",
        ):
            assert option in command
    assert "--run-benchmark" in install[0][-1]
    assert "sudo -n bash" in install[0][-1]
    assert "artifacts/orin/20261020T010203Z/orin-v3.json" in install[0][-1]
    assert fetch[0][-1] == str(bringup_orin.ROOT / "artifacts/orin")


def test_dry_run_prints_commands_without_running_or_writing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    invoked: list[object] = []
    monkeypatch.setattr(bringup_orin.subprocess, "run", lambda *a, **k: invoked.append(a))
    monkeypatch.setattr(bringup_orin.subprocess, "Popen", lambda *a, **k: invoked.append(a))
    assert bringup_orin.main(["--host", "192.0.2.10", "--user", "jetson", "--dry-run"]) == 0

    lines = [line for line in capsys.readouterr().out.splitlines() if line]
    assert lines[0].startswith("# git archive HEAD")
    assert lines[1].startswith("tar -C ") and " | ssh " in lines[1]
    assert lines[2].startswith("ssh ") and lines[3].startswith("scp ")
    assert invoked == []


def test_a_missing_bundle_is_refused_before_connecting(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        bringup_orin.main(
            ["--host", "192.0.2.10", "--user", "jetson", "--bundle", str(tmp_path / "none")]
        )
