from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_current_release_gate_pre8.ps1"
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


def _read() -> str:
    return SCRIPT.read_text(encoding="utf-8")


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell parser is unavailable")
def test_current_release_gate_wrapper_parses_as_powershell() -> None:
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


def test_current_release_gate_wrapper_keeps_explicit_inputs_and_fail_closed_defaults() -> None:
    text = _read()

    for token in (
        "[string]$VersionManifest",
        "[string]$ParityJson",
        "[string]$ReplayEvidence",
        "[string]$SimEvidence",
        "[string]$HilEvidence",
        "[string]$RealEvidence",
        "[string]$ReplaySetManifest",
        "[string]$ReplaySetAccuracyEvidence",
        "[string]$ReplaySetCalibrationVerification",
        "[string]$RealFieldLinkPreflight",
        "[double]$RealFieldLinkMaxAgeSeconds",
        "[string]$FieldEvidenceBundle",
        "[string]$RealAlertLinkBench",
        "[string]$RealGr01OperatorLinkBench",
        "[double]$RealGr01OperatorLinkMaxAgeSeconds",
        "[string]$ModelReviewBundle",
        "[string]$SimulatorSafetyEvidence",
        "[string]$ActuatorSafetyEvidence",
        "[string]$SimulatorScenarioMatrixEvidence",
        "[string]$SimulatorCombinedEvidence",
        "[string]$SimulatorCombinedReleaseModeEvidence",
        "[string]$SimulatorExternalEvidence",
        "[string]$SimulatorExternalEvidenceRevalidation",
        "[string]$Px4AirframeTransactionEvidence",
        "[string]$Px4ReportedPositionResponseMatrixEvidence",
        "[string]$Px4ReportedPositionResponseCampaignEvidence",
        "[string[]]$Px4MissionFeedbackBinding",
        "[string[]]$QgcOperatorVisualAcceptanceEvidence",
        "[string[]]$Simulator3dControlEvidence",
        "[string[]]$SimulatorSceneStreamSetEvidence",
        "--simulator-scene-stream-set-evidence",
        "--px4-mission-feedback-evidence",
        "--px4-reported-position-response-matrix-evidence",
        "--px4-reported-position-response-campaign-evidence",
        "--qgc-operator-visual-acceptance-evidence",
        "MODE|VEHICLE|MISSION_JSON|SIGNED_RUNTIME_PROFILE_JSON|REPORT_JSON",
        "--allow-incomplete",
        "--no-overwrite",
        "--report-only",
        "--field-evidence-bundle",
        "--real-alert-link-bench",
        "--real-gr01-operator-link-bench",
        "--real-gr01-operator-link-max-age-s",
        "--model-review-bundle",
        "--real-field-link-max-age-s",
        "InvariantCulture",
        "Assert-InputFile",
        "Assert-NewOutput",
        "Resolve-Python",
        "Get-Variable -Name LASTEXITCODE",
        "$exitCode = 0",
        "PSScriptRoot",
    ):
        assert token in text

    for path in (
        "release-mode-replay-20260901-pre.8.json",
        "release-mode-sim-20260902-pre.8-combined-qgc-v4.json",
        "release-mode-hil-20260902-target-state-evidence-v1.json",
        "release-mode-real-20260901-pre.8-field-rerun.json",
        "replay-set-positive-fixture-20260924\\replay-set.manifest.json",
        "accuracy-report.json",
        "threshold-calibration-20260924-verification.json",
        "field-link-preflight-current-20260928-r2.json",
        "simulator-safety-acceptance-latest.json",
        "actuator-safety-software-acceptance-latest.json",
        "simulator-scenario-matrix-20260903-portable-paths-pre8.json",
        "combined-sim-qgc-navsat-20261001-r4\\combined-evidence.json",
        "combined-sim-qgc-navsat-20261001-r4\\release-mode-sim-combined-current.json",
        "simulator-external-evidence-policy-helper-20261001-r26.json",
        "simulator-external-evidence-policy-helper-20261001-r26-revalidated.json",
        "px4-reported-position-response-current-parity-20261001-r26\\px4-reported-position-response-matrix-current-parity.json",
        "px4-reported-position-response-current-parity-20261001-r26\\px4-reported-position-response-campaign-current-parity.json",
        "px4-airframe-mission-transaction-acceptance-latest.json",
        "simulator-3d-control-current-20261001-r1\\simulator-3d-control-current-vtol.json",
        "simulator-3d-control-current-20261001-r1\\simulator-3d-control-current-fixed-wing.json",
        "simulator-3d-control-current-20261001-r1\\simulator-3d-control-current-multirotor.json",
        "scene-stream-fixed-wing-20260904.json",
        "scene-stream-multirotor-20260904.json",
        "scene-stream-vtol-20260904.json",
    ):
        assert path in text

    assert (
        "artifacts\\evaluation\\simulator-external-evidence-policy-helper-20261001-r26.json"
        in text
    )
    assert (
        "artifacts\\evaluation\\simulator-external-evidence-policy-helper-20261001-r26-revalidated.json"
        in text
    )
    assert "artifacts\\evaluation\\terminal-continuation-v105\\simulator-3d-control" not in text
    assert "combined-sim-qgc-20260902-hwnd-fallback-v4.json" not in text
    assert "terminal-continuation-v65\\simulator-external-runtime-ai-join" not in text
    assert "terminal-continuation-v65\\simulator-external-runtime-ai-revalidation" not in text
    assert "px4-reported-position-response-matrix-v65-pre8.json" not in text
    assert "px4-reported-position-response-campaign-v65-pre8.json" not in text
    assert "terminal-continuation-v56\\simulator-3d-control" not in text
    assert "simulator-3d-control-v52-dem-ranging" not in text
    assert "simulator-3d-control-v54-odometry" not in text
    assert "simulator-external-runtime-ai-join-20260905-v52-dem-ranging-pre8.json" not in text
    assert (
        "simulator-external-runtime-ai-revalidation-20260905-v52-dem-ranging-pre8.json" not in text
    )
    assert "terminal-continuation-v42\\simulator-external-runtime-ai-join" not in text
    assert "terminal-continuation-v42\\simulator-external-runtime-ai-revalidation" not in text
    assert "terminal-continuation-v43\\simulator-external-runtime-ai-join" not in text
    assert "terminal-continuation-v43\\simulator-external-runtime-ai-revalidation" not in text
    assert "terminal-continuation-v44\\simulator-external-runtime-ai-join" not in text
    assert "terminal-continuation-v44\\simulator-external-runtime-ai-revalidation" not in text
    assert "terminal-continuation-v45\\simulator-external-runtime-ai-join" not in text
    assert "terminal-continuation-v45\\simulator-external-runtime-ai-revalidation" not in text
    assert "terminal-continuation-v46\\simulator-external-runtime-ai-join" not in text
    assert "terminal-continuation-v46\\simulator-external-runtime-ai-revalidation" not in text
    assert "terminal-continuation-v47\\simulator-external-runtime-ai-join" not in text
    assert "terminal-continuation-v47\\simulator-external-runtime-ai-revalidation" not in text
    assert "terminal-continuation-v48\\simulator-external-runtime-ai-join" not in text
    assert "terminal-continuation-v48\\simulator-external-runtime-ai-revalidation" not in text
    assert "terminal-continuation-v49\\simulator-external-runtime-ai-join" not in text
    assert "terminal-continuation-v49\\simulator-external-runtime-ai-revalidation" not in text
    assert (
        "artifacts\\evaluation\\replay-set-positive-fixture-20260924\\"
        "replay-set.manifest.json"
    ) in text
    assert (
        "artifacts\\evaluation\\replay-set-positive-fixture-20260924\\accuracy-report.json"
    ) in text
    assert (
        "artifacts\\evaluation\\replay-set-positive-fixture-20260924\\"
        "threshold-calibration-20260924-verification.json"
    ) in text
    assert "artifacts\\replay-sets\\sim-gazebo-2026\\replay-set.manifest.json" not in text
    assert "accuracy-report-20260903-positive-strata.json" not in text
    assert "threshold-calibration-20260902-pre.8-verification.json" not in text
    assert "artifacts\\evaluation\\field-link-preflight-current-20260928-r2.json" in text
    assert (
        "artifacts\\evaluation\\terminal-continuation-v33\\"
        "field-link-preflight-live-20260904T-current.json"
    ) not in text
    assert (
        "artifacts\\evaluation\\terminal-continuation-v32\\"
        "simulator-external-runtime-ai-join-20260904-current-pre8.json"
    ) not in text
    assert (
        "artifacts\\evaluation\\terminal-continuation-v32\\"
        "simulator-external-runtime-ai-revalidation-20260904-current-pre8.json"
    ) not in text
    assert (
        "artifacts\\evaluation\\terminal-continuation-v34\\"
        "simulator-external-runtime-ai-join-20260904-current-pre8.json"
    ) not in text
    assert (
        "artifacts\\evaluation\\terminal-continuation-v34\\"
        "simulator-external-runtime-ai-revalidation-20260904-current-pre8.json"
    ) not in text

    # The default must point at the current parity-valid evidence chain
    # and the current diagnostic-bound field preflight. Older snapshots are
    # historical provenance only and must not be selected by a no-argument run;
    # the field preflight itself is expected to fail closed when the hardware
    # link is absent or it ages past policy.
    deprecated_provider_fallback = (
        "simulator-external-runtime-ai-revalidation-20260903-"
        + "current-provider-fallback-parity.json"
    )
    assert deprecated_provider_fallback not in text

    # The earlier same-content goal snapshot is historical provenance and must
    # not be selected by a no-argument run either.
    deprecated_revalidation = (
        "simulator-external-runtime-ai-revalidation-20260903-" + "current" + "-" + "goal.json"
    )
    assert deprecated_revalidation not in text

    # The wrapper must bind known evidence paths, not scan artifacts/ and pick
    # a file by mtime or filename pattern.
    assert "Get-ChildItem" not in text
    assert "Sort-Object LastWriteTime" not in text
    assert "-Recurse" not in text

    # The routine command remains report-only while -Strict is the explicit
    # final-release check; this preserves pending field/HIL/REAL gates.
    assert "[switch]$Strict" in text
    assert "if ($Strict)" in text
    assert "exit $strictResult.exit_code" in text
    assert "exit 0" in text


def test_current_release_gate_wrapper_uses_repository_relative_paths_and_unique_outputs() -> None:
    text = _read()

    assert "Join-Path $scriptRoot '..'" in text
    assert "Resolve-RepositoryPath" in text
    assert "release-gate-$fileSuffix-current-$Stamp-v6-bound" in text
    assert "Refusing to overwrite existing release-gate output" in text
    assert "Stamp must use UTC format yyyyMMddTHHmmssZ" in text
    assert "EvaluatedAtUtc" in text
