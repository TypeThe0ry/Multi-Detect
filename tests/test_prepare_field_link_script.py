from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "prepare_field_link.ps1"


def test_field_link_setup_is_parameterized_and_secondary_only() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "[string]$InterfaceAlias = 'Ethernet'" in source
    assert "[string]$IpAddress = '192.168.144.250'" in source
    assert "[string]$AutoSelectCidr = ''" in source
    assert "[switch]$VerifyOnly" in source
    assert "[switch]$PlanOnly" in source
    assert "[switch]$Remove" in source
    assert "[switch]$Elevate" in source
    assert "[string]$Output = ''" in source
    assert "[switch]$AutoSelectInterface" in source
    assert "Resolve-AutoInterfaceAlias" in source
    assert "Test-IPv4InCidr" in source
    assert "$AutoSelectCidr" in source
    assert "multiple active physical interfaces" in source
    assert "No active physical interface has an IPv4 address" in source
    assert "-SkipAsSource $true" in source
    assert "New-NetIPAddress" in source
    assert "Remove-NetIPAddress" in source
    assert "$VerifyOnly" in source
    assert "$Remove" in source
    assert "$Elevate" in source
    assert "Start-Process" in source
    assert "-Verb RunAs" in source
    assert "Test-IsAdministrator" in source
    assert "Write-FieldLinkState" in source
    assert "[System.IO.FileMode]::CreateNew" in source
    assert "schema_version = 1" in source
    assert "schema = 'multidetect.field-link-address-state/v1'" in source
    assert "generated_at_utc" in source
    assert "computer_name" in source
    assert "secondary_address_only = $true" in source
    assert "skip_as_source = $true" in source
    assert "default_gateway_change = $false" in source
    assert "dns_change = $false" in source
    assert "explicit_route_change = $false" in source
    assert "rollback_command" in source
    assert "$appliedChange = $false" in source
    assert "$appliedChange = $true" in source
    assert "$whatIfRequested = [bool]$WhatIfPreference" in source
    assert "mode = $(if ($whatIfRequested) { 'what-if' } else { 'apply' })" in source
    assert "postcondition_checked" in source
    assert "postcondition_passed" in source
    assert "Field-link address postcondition failed" in source
    # The helper must never silently change the default route or resolver.
    assert "-DefaultGateway" not in source
    assert "New-NetRoute" not in source
    assert "Set-DnsClientServerAddress" not in source
