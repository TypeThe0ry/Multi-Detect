from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "provision_multidetect_operator_keys.ps1"


def _read() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_operator_key_provisioner_uses_user_scope_and_cng_randomness() -> None:
    source = _read()

    assert "MULTIDETECT_OPERATOR_KEY" in source
    assert "MULTIDETECT_OPERATOR_MAVLINK_KEY_HEX" in source
    assert '[Environment]::GetEnvironmentVariable($operatorKeyName, "User")' in source
    assert '[Environment]::GetEnvironmentVariable($mavlinkKeyName, "User")' in source
    assert '[Environment]::SetEnvironmentVariable($operatorKeyName, $operatorKey, "User")' in source
    assert '[Environment]::SetEnvironmentVariable($mavlinkKeyName, $mavlinkKey, "User")' in source
    assert "[Security.Cryptography.RandomNumberGenerator]::Create()" in source
    assert source.count(".GetBytes($bytes)") == 2
    assert source.count(".Dispose()") >= 3
    assert source.count("[Array]::Clear(") >= 4


def test_operator_key_provisioner_validates_lengths_and_never_serializes_secrets() -> None:
    source = _read()

    assert "[ValidateRange(32, 4096)]" in source
    assert "[ValidateRange(32, 32)]" in source
    assert "$bytes.Length -ge 32" in source
    assert "$trimmed.Length -eq 64" in source
    assert "$trimmed -match '^[0-9A-Fa-f]{64}$'" in source
    assert "secrets_printed = $false" in source
    assert "ConvertTo-Json -Depth 5" in source
    assert "Write-Output $operatorKey" not in source
    assert "Write-Output $mavlinkKey" not in source
    assert "ConvertTo-Json $operatorKey" not in source
    assert "ConvertTo-Json $mavlinkKey" not in source
    assert "restart_required = $true" in source


def test_operator_key_provisioner_is_idempotent_and_force_rotation_is_explicit() -> None:
    source = _read()

    assert "[switch]$Force" in source
    assert "(-not $Force) -and (Test-OperatorKey" in source
    assert "(-not $Force) -and (Test-MavlinkKey" in source
    assert "force_rotation = [bool]$Force" in source
    assert "if (-not $reuseOperatorKey)" in source
    assert "if (-not $reuseMavlinkKey)" in source
    assert "Restart terminals and QGC" in source
