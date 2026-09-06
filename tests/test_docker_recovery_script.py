from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "recover_docker_desktop_stale_socket.ps1"


def _read_script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_docker_recovery_script_is_explicit_and_read_only_by_default() -> None:
    text = _read_script()

    assert "[CmdletBinding()]" in text
    assert "[switch]$Apply" in text
    assert "[switch]$RemoveStaleEntry" in text
    assert "[switch]$RotateRunDirectory" in text
    assert "[switch]$RestartDocker" in text
    assert "if (-not $Apply)" in text
    assert "Read-only mode: no process, reparse point, or Docker data was changed." in text
    assert "Docker Desktop is still running" in text
    assert "wsl --unregister" not in text
    assert "factory reset" not in text.casefold()


def test_docker_recovery_targets_only_the_confirmed_docker_inference_entry() -> None:
    text = _read_script()

    assert "$entryName = 'dockerInference'" in text
    assert "[Environment]::GetFolderPath('LocalApplicationData')" in text
    assert "Join-Path $localAppData 'Docker\\run'" in text
    assert "[IO.Path]::GetFullPath((Join-Path $runRoot $entryName))" in text
    assert "[IO.FileAttributes]::ReparsePoint" in text
    assert "Unexpected parent directory for recovery target" in text
    assert "Remove-Item -LiteralPath $runRoot" not in text
    assert "Remove-Item -LiteralPath $localAppData" not in text


def test_recovery_prefers_reversible_move_and_gates_last_resort_clear() -> None:
    text = _read_script()

    move_index = text.index("Move-Item -LiteralPath $targetRecord.path")
    fsutil_index = text.index("fsutil.exe reparsepoint delete $targetRecord.path")
    remove_index = text.index("Remove-Item -LiteralPath $targetRecord.path -Force")
    assert move_index < fsutil_index < remove_index
    assert "if (-not $RemoveStaleEntry)" in text
    assert "-Apply -RemoveStaleEntry" in text
    assert "recovery-backup-$stamp" in text


def test_restart_is_hidden_and_only_available_after_apply() -> None:
    text = _read_script()

    assert (
        "if (-not $Apply -and ($RemoveStaleEntry -or $RotateRunDirectory -or $RestartDocker))"
    ) in text
    assert "if ($RestartDocker)" in text
    assert "Start-Process -FilePath $dockerDesktop -WindowStyle Hidden" in text


def test_runtime_directory_rotation_is_reversible_and_does_not_delete_data() -> None:
    text = _read_script()

    assert "run-recovery-backup-$stamp" in text
    assert "Move-Item -LiteralPath $runRoot -Destination $backupRoot -Force" in text
    assert "New-Item -ItemType Directory -Path $runRoot -Force" in text
    assert "Preserved entries:" in text
    assert "Remove-Item -LiteralPath $runRoot" not in text
    assert "wsl --unregister" not in text
