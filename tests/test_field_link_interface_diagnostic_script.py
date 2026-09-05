from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "inspect_field_link_interfaces.ps1"


def test_interface_diagnostic_is_read_only_and_structured() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    for token in (
        "[string]$ExpectedSourceCidr = '192.168.144.0/24'",
        "[string]$Output = ''",
        "Get-NetAdapter -Physical",
        "Get-NetIPAddress -InterfaceIndex",
        "Get-NetRoute -InterfaceIndex",
        "physical_query_fallback",
        "target_cidr_addresses",
        "target_route_present",
        "selection = [ordered]@",
        "candidate_aliases",
        "read_only = $true",
        "ConvertTo-Json",
        "PSScriptRoot",
        "Resolve-RepositoryPath",
    ):
        assert token in source

    # The diagnostic may recommend the existing address helper, but it must
    # never mutate an adapter, address, route, or resolver itself.
    for forbidden in (
        "New-NetIPAddress",
        "Remove-NetIPAddress",
        "Set-NetIPAddress",
        "New-NetRoute",
        "Remove-NetRoute",
        "Set-NetRoute",
        "Set-DnsClientServerAddress",
        "-Verb RunAs",
    ):
        assert forbidden not in source


def test_interface_diagnostic_normalizes_cidr_and_keeps_disconnected_adapters() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "Parse-IPv4Cidr" in source
    assert "normalized_source_cidr" in source
    assert "including disconnected" in source
    assert "status -eq 'Up'" in source
    assert "ready_candidate" in source
