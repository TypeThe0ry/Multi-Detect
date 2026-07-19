[CmdletBinding()]
param(
    [ValidateSet("Status", "Start", "Stop", "Restart", "Enable", "Disable", "Logs")]
    [string]$Action = "Status",
    [string]$HostAlias = "multidetect-jetson",
    [ValidateRange(1, 2000)]
    [int]$Lines = 100,
    [switch]$Follow
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ssh = Get-Command ssh.exe -ErrorAction Stop
$unit = "multidetect-live.service"
$requiresSudo = $Action -in @("Start", "Stop", "Restart", "Enable", "Disable")

switch ($Action) {
    "Status" {
        $remote = @"
set -eu
printf 'enabled='; systemctl is-enabled $unit || true
printf 'active='; systemctl is-active $unit || true
systemctl show $unit -p ActiveState -p SubState -p MainPID -p NRestarts --no-pager
"@
    }
    "Start" { $remote = "sudo systemctl start $unit; systemctl --no-pager --full status $unit" }
    "Stop" { $remote = "sudo systemctl stop $unit; systemctl --no-pager --full status $unit || true" }
    "Restart" { $remote = "sudo systemctl restart $unit; systemctl --no-pager --full status $unit" }
    "Enable" { $remote = "sudo systemctl enable $unit; systemctl is-enabled $unit" }
    "Disable" { $remote = "sudo systemctl disable $unit; systemctl is-enabled $unit || true" }
    "Logs" {
        $followFlag = if ($Follow) { "-f" } else { "" }
        $remote = "journalctl -u $unit -n $Lines --no-pager $followFlag"
    }
}

$sshArgs = @("-o", "BatchMode=yes", "-o", "ConnectTimeout=10")
if ($requiresSudo -or $Follow) {
    # Keep sudo prompts local to this explicit operator action; the helper never
    # reads or prints the protected Jetson environment file.
    $sshArgs += "-tt"
}
$sshArgs += @($HostAlias, $remote)

& $ssh.Source @sshArgs
if ($LASTEXITCODE -ne 0) {
    throw "Jetson service action '$Action' failed with exit code $LASTEXITCODE."
}
