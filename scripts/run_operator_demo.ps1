param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Args)
$ErrorActionPreference = 'Stop'
$env:PYTHONPATH = "src$([IO.Path]::PathSeparator)."
$env:PYTHONUTF8 = '1'
& python (Join-Path $PSScriptRoot 'run_operator_demo.py') @Args
exit $LASTEXITCODE
