from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_field_link_preflight.ps1"


def test_windows_field_link_wrapper_is_read_only_and_verifies_saved_report() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    for token in (
        "prepare_field_link.ps1",
        "inspect_field_link_interfaces.ps1",
        "-VerifyOnly",
        "run_field_link_preflight.py",
        "verify_field_link_preflight.py",
        "JetsonHost",
        "AutoSelectInterface",
        "Gr01Host",
        "CameraHost",
        "InterfaceDiagnostic",
        "GenerateInterfaceDiagnostic",
        "JetsonPort",
        "Gr01Port",
        "RtspPort",
        "MetadataPort",
        "QgcPort",
        "--jetson-host",
        "--gr01-host",
        "--camera-host",
        "--interface-diagnostic",
        "--interface-alias",
        "--jetson-port",
        "--gr01-port",
        "--rtsp-port",
        "--metadata-port",
        "--qgc-port",
        "--expected-source-cidr",
        "--require-route",
        "--protocol-checks",
        "--stop-on-failure",
        "--discover-cidr",
        "--discover-max-hosts",
        "--discover-workers",
        "--discover-timeout-s",
        "--allow-unready",
        "--require-all",
        "--out",
    ):
        assert token in text

    # Interface selection is opt-in.  The wrapper may ask the read-only
    # prepare helper to select a single active physical adapter on the target
    # aircraft subnet, but it must retain the explicit alias path by default.
    assert "if ($AutoSelectInterface)" in text
    assert "-AutoSelectCidr $ExpectedSourceCidr" in text
    assert "-AutoSelectInterface -VerifyOnly" in text

    # A migrated workstation can request a fresh read-only interface snapshot
    # in the same invocation, preventing a stale JSON file from being bound to
    # the endpoint probes.
    assert "if ($GenerateInterfaceDiagnostic)" in text
    assert "Generated read-only interface diagnostic" in text
    assert "-ExpectedSourceCidr $ExpectedSourceCidr" in text

    # Address changes belong exclusively to prepare_field_link.ps1.  This
    # orchestration wrapper must never mutate the workstation network state.
    for forbidden in (
        "New-NetIPAddress",
        "Remove-NetIPAddress",
        "Set-NetIPAddress",
        "New-NetRoute",
        "Remove-NetRoute",
    ):
        assert forbidden not in text


def test_windows_field_link_wrapper_resolves_paths_from_repository() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "PSScriptRoot" in text
    assert "Resolve-RepositoryPath" in text
    assert "repositoryRoot" in text
    assert "hardware_link_ready=false" in text


def test_windows_field_link_wrapper_reads_native_exit_codes_defensively() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    # StrictMode must not fail merely because a PowerShell host did not create
    # the automatic LASTEXITCODE variable for an empty native command output.
    # Both the Python helpers and the read-only interface probe use the same
    # zero-default/fallback pattern.
    assert "Get-Variable -Name LASTEXITCODE -ErrorAction SilentlyContinue" in text
    assert "$exitCode = 0" in text
    assert "$stateExitCode = 0" in text
    assert "$stateLastExitCodeVariable" in text


def test_windows_field_link_wrapper_keeps_discovery_explicit_and_bounded() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    # Discovery is opt-in and must remain a bounded diagnostic.  The wrapper
    # forwards the explicit CIDR and limits only when DiscoverCidr is present;
    # it never promotes a candidate host to a configured endpoint.  Explicit
    # endpoint flags are always forwarded so migration to another subnet does
    # not silently probe stale defaults.
    for token in (
        "$DiscoverCidr",
        "$DiscoverMaxHosts",
        "$DiscoverWorkers",
        "$DiscoverTimeoutSeconds",
        "DiscoverTimeoutSeconds requires DiscoverCidr",
        "if (-not [string]::IsNullOrWhiteSpace($DiscoverCidr))",
        "Discovery is opt-in",
    ):
        assert token in text

    # A field operator can request a bounded fail-fast watch without waiting
    # through the remaining samples after the first link failure.  The
    # wrapper must forward the switch to the Python watch implementation; the
    # saved partial report remains fail-closed and is still verifiable.
    assert "$StopOnFailure" in text
    assert "if ($StopOnFailure)" in text
    assert "$preflightArguments.Add('--stop-on-failure')" in text
    for token in (
        "--jetson-host",
        "--gr01-host",
        "--camera-host",
        "--jetson-port",
        "--gr01-port",
        "--rtsp-port",
        "--metadata-port",
        "--qgc-port",
    ):
        assert token in text

    # The defaults stay aligned with the Python preflight CLI and all values
    # are forwarded as strings, so PowerShell 5.1 cannot reinterpret an IPv4
    # endpoint or silently drop a port while constructing the argument list.
    for value in (
        "192.168.144.20",
        "192.168.144.11",
        "192.168.144.108",
        "22",
        "5760",
        "554",
        "14580",
        "14581",
    ):
        assert value in text
