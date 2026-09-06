from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

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
        "[switch]$PlanOnly",
        "[string]$PlanOutput",
        "MULTIDETECT_OPERATOR_HMAC_KEY",
        "MULTIDETECT_MAVLINK_SIGNING_KEY_HEX",
        "--hardware-mode",
        "--require-passed",
        "-RealGr01OperatorLinkBench",
        "-RealGr01OperatorLinkMaxAgeSeconds",
        "verify_gr01_release_session.py",
        "run_multidetect_cli.py",
        "'create'",
        "'verify'",
        "Path escapes repository root",
        "Session output already exists",
        "InvariantCulture",
        "cannot override controlled parameter",
        "verify_gr01_release_session_plan.py",
        "--check-environment",
        "--require-ready",
        "This record verifies the GR01 E7 binding only",
    ):
        assert token in text

    assert text.index("'gr01-link-bench.json'") < text.index("'gr01-release-session.json'")
    assert text.index("'gr01-release-session-plan.json'") < text.index("'gr01-link-bench.json'")
    assert text.index("'create'") < text.index("'verify'")
    assert "MULTIDETECT_OPERATOR_HMAC_KEY=" not in text
    assert "MULTIDETECT_MAVLINK_SIGNING_KEY_HEX=" not in text


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is unavailable")
def test_plan_only_writes_closed_no_network_receipt_without_secrets() -> None:
    temp_root = ROOT / "tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.pop("MULTIDETECT_OPERATOR_HMAC_KEY", None)
    environment.pop("MULTIDETECT_MAVLINK_SIGNING_KEY_HEX", None)
    with tempfile.TemporaryDirectory(prefix="gr01-plan-test-", dir=temp_root) as directory:
        output_root = Path(directory)
        plan = output_root / "plan.json"
        result = subprocess.run(
            [
                str(POWERSHELL),
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(SCRIPT),
                "-HostAddress",
                "192.168.144.20",
                "-HardwareId",
                "GR01-PLAN-TEST",
                "-ExpectedSourceCidr",
                "192.168.144.0/24",
                "-RepositoryRoot",
                str(ROOT),
                "-Python",
                sys.executable,
                "-OutputDirectory",
                str(output_root),
                "-PlanOnly",
                "-PlanOutput",
                str(plan),
            ],
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr or result.stdout
        assert [item.name for item in output_root.iterdir()] == ["plan.json"]
        document = json.loads(plan.read_text(encoding="utf-8"))
        schema = json.loads(
            (ROOT / "configs/schemas/gr01-release-session-plan.schema.json").read_text(
                encoding="utf-8"
            )
        )
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)
        assert document["ready_to_execute"] is False
        assert document["schema"] == "multidetect.gr01-release-session-plan/v2"
        assert document["claims"]["network_contacted"] is False
        assert document["claims"]["hardware_observed"] is False
        assert document["source_identity"]["repository"]["git_head"]
        assert document["source_identity"]["release_parity"]["path"] == (
            "configs/release-parity.json"
        )
        assert document["secret_environment"] == {
            "operator_hmac": {"name": "MULTIDETECT_OPERATOR_HMAC_KEY", "present": False},
            "mavlink_signing": {
                "name": "MULTIDETECT_MAVLINK_SIGNING_KEY_HEX",
                "present": False,
            },
        }
