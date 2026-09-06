from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_gr01_release_session.ps1"
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell parser is unavailable")
def test_gr01_release_session_wrapper_parses_as_powershell() -> None:
    escaped_path = str(SCRIPT).replace("'", "''")
    command = (
        "$tokens = $null; $parseErrors = $null; "
        f"[System.Management.Automation.Language.Parser]::ParseFile('{escaped_path}', "
        "[ref]$tokens, [ref]$parseErrors) | Out-Null; "
        "if ($parseErrors.Count -ne 0) { "
        "$parseErrors | ForEach-Object { [Console]::Error.WriteLine($_.ToString()) }; exit 1 }; "
        "exit 0"
    )
    result = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_gr01_release_session_wrapper_is_transactional_and_fail_closed() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    for token in (
        "[string]$HostAddress",
        "[string]$HardwareId",
        "[string]$ExpectedSourceCidr",
        "MULTIDETECT_OPERATOR_HMAC_KEY",
        "MULTIDETECT_MAVLINK_SIGNING_KEY_HEX",
        "--hardware-mode",
        "--require-passed",
        "-RealGr01OperatorLinkBench",
        "-RealGr01OperatorLinkMaxAgeSeconds",
        "verify_gr01_release_session.py",
        "'create'",
        "'verify'",
        "Path escapes repository root",
        "Session output already exists",
        "InvariantCulture",
        "cannot override controlled parameter",
        "This record verifies the GR01 E7 binding only",
    ):
        assert token in text

    assert text.index("'gr01-link-bench.json'") < text.index("'gr01-release-session.json'")
    assert text.index("'create'") < text.index("'verify'")
    assert "MULTIDETECT_OPERATOR_HMAC_KEY=" not in text
    assert "MULTIDETECT_MAVLINK_SIGNING_KEY_HEX=" not in text
