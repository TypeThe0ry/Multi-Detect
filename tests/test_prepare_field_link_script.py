from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "prepare_field_link.ps1"


def test_field_link_setup_is_parameterized_and_secondary_only() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "[string]$InterfaceAlias = 'Ethernet'" in source
    assert "[string]$IpAddress = '192.168.144.250'" in source
    assert "[string]$AutoSelectCidr = ''" in source
    assert "[switch]$VerifyOnly" in source
    assert "[switch]$Remove" in source
    assert "[switch]$Elevate" in source
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
    # The helper must never silently change the default route or resolver.
    assert "-DefaultGateway" not in source
    assert "New-NetRoute" not in source
    assert "Set-DnsClientServerAddress" not in source
