from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_goal_acceptance.ps1"
REPLAY_DOC = ROOT / "docs" / "replay-set-manifest.md"
RELEASE_GATE_DOC = ROOT / "docs" / "release-gate.md"


def _read() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _read_replay_doc_binding_command() -> str:
    source = REPLAY_DOC.read_text(encoding="utf-8")
    match = re.search(
        r"### 绑定到总 goal-acceptance.*?```powershell\n(.*?)\n```",
        source,
        flags=re.DOTALL,
    )
    assert match is not None, "REPLAY docs must contain a goal-acceptance PowerShell command"
    return match.group(1)


def _read_current_release_gate_command() -> str:
    source = RELEASE_GATE_DOC.read_text(encoding="utf-8")
    match = re.search(
        r"## 当前 E7 绑定复核.*?后续复核直接复用同一组显式输入.*?```powershell\n(.*?)\n```",
        source,
        flags=re.DOTALL,
    )
    assert match is not None, "release-gate docs must contain the current E7 command"
    return match.group(1)


def test_goal_acceptance_binds_qgc_build_to_canonical_product_version() -> None:
    source = _read()

    assert "[string]$CoreRoot = ''" in source
    assert "[string]$QgcRoot = ''" in source
    assert "$scriptRoot = [string]$PSScriptRoot" in source
    assert "$MyInvocation.MyCommand.Path" in source
    assert "[string]$FixedWingAcceptanceOutput = ''" in source
    assert "[string]$Mode2PayloadAcceptanceOutput = ''" in source
    assert "[string]$Mode3ApproachAcceptanceOutput = ''" in source
    assert "[string]$SimulatorSafetyOutput = ''" in source
    assert "[string]$Px4AirframeTransactionOutput = ''" in source
    assert "[string]$SimulatorScenarioMatrixOutput = ''" in source
    assert "[string]$Simulator3dVtolOutput = ''" in source
    assert "[string]$Simulator3dFixedWingOutput = ''" in source
    assert "[string]$Simulator3dMultirotorOutput = ''" in source
    assert "[string]$TestTempRoot = ''" in source
    assert "[string]$ReplaySetManifest = ''" in source
    assert "[string]$ReplaySetAccuracyReport = ''" in source
    assert "[string]$ReplaySetReadinessOutput = ''" in source
    assert "[switch]$RequireReplaySet" in source
    assert "function Test-IcmpReachable" in source
    assert "System.Net.NetworkInformation.Ping" in source
    assert "Test-Connection -ComputerName" not in source
    assert "System.Text.UTF8Encoding($false)" in source
    assert "System.IO.File]::WriteAllText" in source
    assert "$QgcBuildRoot = ''" in source
    assert "configs\\product-version.json" in source
    assert "Get-Content -LiteralPath $versionManifestPath" in source
    assert "[switch]$ComputerUseOperatorAuthorized" in source
    assert "[switch]$AllowMissingOperatorCredentials" in source
    assert "[switch]$RequireSimulator3dControlEvidence" in source
    assert (
        "$calibrationBoard = Join-Path $CoreRoot "
        '"configs/calibration/charuco-7x5-40mm-20mm-4200px.png"'
    ) in source
    assert "artifacts/calibration/charuco-7x5-40mm-20mm.png" not in source
    assert (
        'Join-Path $CoreRoot ("build\\windows-v{0}-release" -f $productVersion.release_label)'
    ) in source
    assert ('Join-Path $QgcBuildRoot ("portable-v{0}" -f $productVersion.release_label)') in source
    assert "v0.2.0" not in source


def test_goal_acceptance_records_and_checks_version_identity() -> None:
    source = _read()

    assert "QGC_APP_VERSION_OVERRIDE:STRING" in source
    assert "QGC_APP_RELEASE_LABEL:STRING" in source
    assert "product_version = [ordered]@{" in source
    assert "qgc_build_root = $QgcBuildRoot" in source
    assert "portable_root = $portableRoot" in source


def test_goal_acceptance_records_source_snapshot_provenance() -> None:
    source = _read()

    assert "function Get-GitProvenance" in source
    assert "rev-parse --verify HEAD" in source
    assert "status --porcelain=v1 --untracked-files=all" in source
    assert "dirty_entry_count = $statusCount" in source
    assert "status_sha256 = $statusSha" in source
    assert "snapshot_kind = if ($worktreeClean)" in source
    assert "$gitProvenance = Get-GitProvenance -RepositoryRoot $CoreRoot" in source
    assert "git_provenance = $gitProvenance" in source


def test_goal_acceptance_reads_native_exit_codes_defensively_under_strict_mode() -> None:
    source = _read()

    # Every native command result must flow through the helper.  A direct
    # LASTEXITCODE read can itself throw in StrictMode when a nested
    # powershell.exe host did not materialize the automatic variable.
    assert "function Get-NativeExitCode" in source
    assert "Get-Variable -Name LASTEXITCODE -ErrorAction SilentlyContinue" in source
    assert "return 0" in source
    assert "return [int]$lastExitCodeVariable.Value" in source
    assert "[int]$LASTEXITCODE" not in source
    assert "$LASTEXITCODE -ne 0" not in source


def test_goal_acceptance_runs_fixed_wing_and_native_qgc_style_gates() -> None:
    source = _read()

    assert (
        "$fixedWingAcceptanceScript = Join-Path $CoreRoot "
        '"scripts/run_fixed_wing_software_acceptance.py"'
    ) in source
    assert (
        "$fixedWingAcceptanceEvidence = Join-Path $CoreRoot "
        '"artifacts/evaluation/fixed-wing-software-acceptance-latest.json"'
    ) in source
    assert (
        "$mode2PayloadAcceptanceScript = Join-Path $CoreRoot "
        '"scripts/run_mode2_payload_hil_acceptance.py"'
    ) in source
    assert (
        "$mode2PayloadAcceptanceEvidence = Join-Path $CoreRoot "
        '"artifacts/evaluation/mode2-payload-hil-acceptance-latest.json"'
    ) in source
    assert (
        "$mode3ApproachAcceptanceScript = Join-Path $CoreRoot "
        '"scripts/run_mode3_approach_hil_acceptance.py"'
    ) in source
    assert (
        "$mode3ApproachAcceptanceEvidence = Join-Path $CoreRoot "
        '"artifacts/evaluation/mode3-approach-hil-acceptance-latest.json"'
    ) in source
    assert (
        '$simSafetyScript = Join-Path $CoreRoot "scripts/run_simulator_safety_acceptance.py"'
    ) in source
    assert (
        '$simSafetyVerifier = Join-Path $CoreRoot "scripts/verify_simulator_safety_evidence.py"'
    ) in source
    assert (
        "$simSafetyEvidence = Join-Path $CoreRoot "
        '"artifacts/evaluation/simulator-safety-acceptance-latest.json"'
    ) in source
    assert (
        "$px4AirframeTransactionScript = Join-Path $CoreRoot "
        '"scripts/run_px4_airframe_transaction_acceptance.py"'
    ) in source
    assert (
        "$px4AirframeTransactionVerifier = Join-Path $CoreRoot "
        '"scripts/verify_px4_airframe_transaction_acceptance.py"'
    ) in source
    assert (
        "$px4AirframeTransactionEvidence = Join-Path $CoreRoot "
        '"artifacts/evaluation/px4-airframe-mission-transaction-acceptance-latest.json"'
    ) in source
    assert (
        '$simScenarioMatrixScript = Join-Path $CoreRoot "scripts/run_simulator_scenario_matrix.py"'
    ) in source
    assert (
        "$simScenarioMatrixVerifier = Join-Path $CoreRoot "
        '"scripts/verify_simulator_scenario_matrix.py"'
    ) in source
    assert (
        "$simScenarioMatrixEvidence = Join-Path $CoreRoot "
        '"artifacts/evaluation/simulator-scenario-matrix-acceptance-latest.json"'
    ) in source
    assert (
        '$sim3dControlScript = Join-Path $CoreRoot "scripts/run_simulator_3d_control_acceptance.py"'
    ) in source
    assert (
        "$sim3dControlVerifier = Join-Path $CoreRoot "
        '"scripts/verify_simulator_3d_control_evidence.py"'
    ) in source
    assert "function Get-Simulator3dControlBinding" in source
    assert '"--bind"' in source
    assert '"simulator_3d_control_binding_verified"' in source
    assert "simulator_3d_control_acceptance = $sim3dBinding" in source
    assert "function Resolve-OutputArtifactPath" in source
    assert "-Requested $FixedWingAcceptanceOutput" in source
    assert "-Requested $Mode2PayloadAcceptanceOutput" in source
    assert "-Requested $Mode3ApproachAcceptanceOutput" in source
    assert "-Requested $SimulatorSafetyOutput" in source
    assert "-Requested $Px4AirframeTransactionOutput" in source
    assert "-Requested $SimulatorScenarioMatrixOutput" in source
    assert "-Requested $Simulator3dVtolOutput" in source
    assert "-Requested $Simulator3dFixedWingOutput" in source
    assert "-Requested $Simulator3dMultirotorOutput" in source
    assert "-Requested $TestTempRoot" in source
    assert "$env:TEMP = $pytestTempRoot" in source
    assert "$env:TMP = $pytestTempRoot" in source
    assert '"--basetemp", $pytestTempRoot' in source
    assert (
        '"-m", "pytest", "-q", "--basetemp", $pytestTempRoot, '
        '"tests/test_qgc_native_flyview_style.py"'
    ) in source
    assert '$fixedWingAcceptanceScript "--out" $fixedWingAcceptanceEvidence' in source
    assert "$mode2PayloadAcceptanceScript" in source
    assert "$mode3ApproachAcceptanceScript" in source
    assert "mode2_payload = [ordered]@{" in source
    assert "mode3_approach = [ordered]@{" in source
    assert "hil_contract_acceptance = [ordered]@{" in source
    assert "all_contracts_passed = $true" in source
    assert "mode2_payload_hil_acceptance_passed" in source
    assert "mode3_approach_hil_acceptance_passed" in source
    assert '$simSafetyScript "--out" $simSafetyEvidence' in source
    assert "$simSafetyVerifier $simSafetyEvidence" in source
    assert '$px4AirframeTransactionScript "--out" $px4AirframeTransactionEvidence' in source
    assert "$px4AirframeTransactionVerifier $px4AirframeTransactionEvidence" in source
    assert (
        '$simScenarioMatrixScript "--cover-faults" "--fault-tail" "1.0" "--out" '
        "$simScenarioMatrixEvidence"
    ) in source
    assert "$simScenarioMatrixEvidenceObject.fault_coverage_required" in source
    assert (
        "requested_frames_before_fault_coverage = [int]$simScenarioMatrixEvidenceObject" in source
    )
    assert "fault_coverage = @($simScenarioMatrixEvidenceObject.scenarios" in source
    assert (
        '$simScenarioMatrixVerifier $simScenarioMatrixEvidence "--expected-release-label" '
        "$productVersion.release_label"
    ) in source
    assert "simulator_safety_acceptance = [ordered]@{" in source
    assert "simulator_scenario_matrix_acceptance = [ordered]@{" in source
    assert "px4_airframe_transaction_acceptance = [ordered]@{" in source
    assert "simulator_safety_acceptance_finished" in source
    assert "SIM safety evidence verifier did not pass" in source
    assert "fixed_wing_software_acceptance = [ordered]@{" in source
    assert (
        '$actuatorSafetyScript = Join-Path $CoreRoot "scripts/run_actuator_safety_acceptance.py"'
    ) in source
    assert (
        '$actuatorSafetyVerifier = Join-Path $CoreRoot "scripts/verify_actuator_safety_evidence.py"'
    ) in source
    assert (
        "$actuatorSafetyEvidence = Join-Path $CoreRoot "
        '"artifacts/evaluation/actuator-safety-software-acceptance-latest.json"'
    ) in source
    assert '$actuatorSafetyScript "--out" $actuatorSafetyEvidence' in source
    assert '$actuatorSafetyVerifier $actuatorSafetyEvidence "--expected-release-label"' in source
    assert "actuator_safety_acceptance = [ordered]@{" in source
    assert "actuator_safety_software_acceptance_finished" in source
    assert "qgc_native_style_contract = [ordered]@{" in source
    assert 'surface = "native_qgc_flyview"' in source
    assert "computer_use_operator_authorized = [bool]$ComputerUseOperatorAuthorized" in source
    assert "computer_use_tool_exposed = $false" in source
    assert 'operator_visual_acceptance = "not_claimed"' in source
    assert (
        'software_status = if ($operatorCredentialsReady) { "pass" } '
        'else { "pass_with_environment_gap" }'
    ) in source
    assert "gate_overridden = [bool]$AllowMissingOperatorCredentials" in source
    assert "$remainingLiveGates = @(" in source
    assert "remaining_live_gates = $remainingLiveGates" in source
    assert "if ([bool]$simulatorExternalBinding.gate_passed)" in source
    assert (
        "Complete manual QGC Fly View visual acceptance for the three external PX4/Gazebo scenarios"
        in source
    )
    assert (
        "Run external PX4/Gazebo 3-D runtime for VTOL, fixed-wing and multirotor "
        "and complete manual QGC Fly View acceptance" in source
    )
    assert "$(if (-not $operatorCredentialsReady)" not in source
    assert '$qmlCacheGen = Join-Path $qtBin "qmlcachegen.exe"' in source
    assert 'Invoke-Checked $qmlCacheGen @("--bare", (Join-Path $QgcRoot $qml))' in source
    assert (
        "$qgcCustomCollection = & $python -m pytest --collect-only -q "
        "--basetemp $pytestTempRoot tools/tests/test_multidetect_custom_app.py"
    ) in source
    assert (
        '$qgcCustomTests = @($qgcCustomCollection | Where-Object { $_ -match "::" }).Count'
    ) in source
    assert "qgc_custom_tests = $qgcCustomTests" in source
    assert (
        "mavlink_key_hex_format_valid = $operatorMavlinkKeyTrimmed -match '^[0-9A-Fa-f]{64}$'"
    ) in source
    assert (
        "mavlink_key_hex_format_valid = [bool]$operatorCredentialReadiness."
        "mavlink_key_hex_format_valid"
    ) in source


def test_goal_acceptance_diff_check_handles_crlf_worktrees() -> None:
    source = _read()

    assert source.count('"core.whitespace=cr-at-eol"') == 2


def test_goal_acceptance_prefers_aircraft_or_active_physical_ethernet() -> None:
    source = _read()

    # A migrated Windows workstation can expose a disconnected TAP adapter
    # before the real NIC.  The evidence must select the aircraft subnet or an
    # active physical adapter instead of blindly taking the first item.
    assert "$physicalEthernetProfiles = @(" in source
    assert "Get-NetIPAddress -InterfaceIndex $adapter.ifIndex" in source
    assert (
        "aircraft_subnet = [bool]($ipv4 | Where-Object { $_ -match '^192\\.168\\.144\\.' })"
    ) in source
    assert "Sort-Object" in source
    assert "if ($_.aircraft_subnet) { 0 } elseif ($_.is_up) { 1 } else { 2 }" in source
    assert "ethernet_ipv4 = @($physicalEthernetIpv4)" in source
    assert "physical_adapters = @(" in source


def test_ephemeral_acceptance_roots_stay_out_of_git_provenance() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

    assert "/.artifacts/" in gitignore
    assert "/.tmp-*" in gitignore
    assert "/.tmp-pytest-*/" in gitignore


def test_goal_acceptance_can_bind_and_require_saved_field_link_preflight() -> None:
    source = _read()

    assert "[string]$FieldLinkPreflight = ''" in source
    assert "[double]$FieldLinkMaxAgeSeconds = 900.0" in source
    assert "[switch]$RequireFieldLinkPreflight" in source
    assert "function Get-FieldLinkPreflightBinding" in source
    assert "[Parameter(Mandatory)] [AllowEmptyString()] [string]$ReportPath" in source
    assert "scripts/verify_field_link_preflight.py" in source
    assert "-Required ([bool]$RequireFieldLinkPreflight)" in source
    assert "verification_exit_code" in source
    assert "field_link_preflight = $fieldLinkBinding" in source
    assert "field_link_ready = [bool]$fieldLinkBinding.hardware_link_ready" in source
    assert "field_link_gate_passed = [bool]$fieldLinkBinding.gate_passed" in source
    assert "route_checks_requested" in source
    assert "route_checks_passed" in source
    assert "route_source_cidr" in source
    assert "'--max-age-s'" in source
    assert "report_timestamp_epoch_s" in source
    assert "report_age_s" in source
    assert "freshness" in source
    assert "if ([bool]$fieldLinkBinding.provided)" in source
    assert 'throw "Field-link preflight is not ready' in source
    assert (
        'field-link preflight path is required")\n'
        "            throw ($binding.reasons -join '; ')" in source
    )


def test_goal_acceptance_binds_replay_set_readiness() -> None:
    source = _read()

    assert "function Get-ReplaySetReadinessBinding" in source
    assert "scripts/check_replay_set_readiness.py" in source
    assert "-ManifestPath $ReplaySetManifest" in source
    assert "-AccuracyReportPath $ReplaySetAccuracyReport" in source
    assert "-ReadinessOutputPath $ReplaySetReadinessOutput" in source
    assert "-Required ([bool]$RequireReplaySet)" in source
    assert "replay_set_readiness = $replaySetBinding" in source
    assert "manifest_gate_passed" in source
    assert "accuracy_gate_passed" in source
    assert "blocking_reasons" in source
    assert "next_actions" in source
    assert "verification_exit_code" in source
    assert "Replay-set readiness is not ready" in source
    assert (
        "Provide a complete six-stratum deployment replay-set with reviewed truth/predictions "
        "and an accuracy report"
    ) in source


def test_replay_docs_keep_current_goal_acceptance_provenance_bound() -> None:
    command = _read_replay_doc_binding_command()

    # The command writes the current v6 snapshot.  It must therefore retain all
    # provenance inputs that make that snapshot meaningful, not just the
    # REPLAY readiness node.
    for argument in (
        "-ReplaySetManifest",
        "-ReplaySetReadinessOutput",
        "-EvidencePath",
        "-FieldLinkPreflight",
        "-SimulatorExternalEvidence",
        "-SimulatorExternalRevalidationOutput",
        "-CombinedSimEvidence",
        "-CombinedSimReleaseModeEvidence",
        "-RequireSimulatorExternalEvidence",
        "-RequireCombinedSimEvidence",
    ):
        assert argument in command
    assert "-AllowMissingOperatorCredentials" not in command
    assert "multidetect-goal-acceptance-20260903-replay-readiness-v6-strict.json" in command


def test_release_gate_docs_keep_current_e7_inputs_explicit() -> None:
    command = _read_current_release_gate_command()

    for argument in (
        "--parity-json",
        "--replay-evidence",
        "--sim-evidence",
        "--hil-evidence",
        "--real-evidence",
        "--replay-set-manifest",
        "--replay-set-accuracy-evidence",
        "--replay-set-calibration-verification",
        "--real-field-link-preflight",
        "--simulator-safety-evidence",
        "--actuator-safety-evidence",
        "--simulator-scenario-matrix-evidence",
        "--simulator-combined-evidence",
        "--simulator-combined-release-mode-evidence",
        "--px4-airframe-transaction-evidence",
        "--simulator-3d-control-evidence",
        "--allow-incomplete",
        "--no-overwrite",
        "scripts\\check_release_gate.py",
        "--report-only",
    ):
        assert argument in command
    assert "$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')" in command
    assert "$evaluatedAt = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')" in command
    assert (
        '$manifestOut = "artifacts\\evaluation\\release-gate-pre.8-current-$stamp-v6-bound.json"'
    ) in command
    assert (
        '$reportOut = "artifacts\\evaluation\\release-gate-pre.8-current-'
        '$stamp-v6-bound-report.json"'
    ) in command
    assert "--out $manifestOut" in command
    assert "--out $reportOut" in command
    assert "--evaluated-at-utc $evaluatedAt" in command
    assert "--AllowMissingOperatorCredentials" not in command


def test_goal_acceptance_can_bind_and_require_combined_sim_evidence() -> None:
    source = _read()

    assert "[string]$CombinedSimEvidence = ''" in source
    assert "[string]$CombinedSimReleaseModeEvidence = ''" in source
    assert "[string]$SimulatorExternalEvidence = ''" in source
    assert "[string]$SimulatorExternalRevalidationOutput = ''" in source
    assert "[switch]$RequireCombinedSimEvidence" in source
    assert "[switch]$RequireSimulatorExternalEvidence" in source
    assert "function Get-CombinedSimBinding" in source
    assert "scripts/verify_combined_sim_evidence.py" in source
    assert "-Required ([bool]$RequireCombinedSimEvidence)" in source
    assert "--expected-release-label" in source
    assert "--release-mode-evidence" in source
    assert '"--require-release-mode"' in source
    assert "simulator_combined_acceptance = $combinedSimBinding" in source
    assert "function Get-SimulatorExternalEvidenceBinding" in source
    assert "scripts/verify_simulator_external_evidence.py" in source
    assert "-JoinPath $SimulatorExternalEvidence" in source
    assert "-RevalidationPath $simulatorExternalRevalidationEvidence" in source
    assert "-Required ([bool]$RequireSimulatorExternalEvidence)" in source
    assert '"--join" $binding.path' in source
    assert '"--expected-release-label" ([string]$productVersion.release_label)' in source
    assert "simulator_external_evidence = $simulatorExternalBinding" in source
    assert "simulator-external-runtime-ai-revalidation-acceptance-latest.json" in source
    assert "combined SIM evidence requires both source and release-mode paths" in source
    assert (
        'combined SIM evidence and release-mode envelope are required")\n'
        "            throw ($binding.reasons -join '; ')" in source
    )
