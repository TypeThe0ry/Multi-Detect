param(
    [string]$Source = "0",
    [ValidateSet("CPU", "CUDA", "Auto")]
    [string]$Provider = "CPU",
    [ValidateRange(0, 2147483647)]
    [int]$MaxFrames = 0,
    [switch]$NoDisplay
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$TorchLib = Join-Path $Root ".venv-train\Lib\site-packages\torch\lib"
$Executable = Join-Path $Root ".venv\Scripts\multi-detect.exe"
$FireModel = Join-Path $Root "artifacts\training\hardneg-snapshots\v5-local-calibrated\best.onnx"
$FireManifest = Join-Path $Root "artifacts\training\hardneg-snapshots\v5-local-calibrated\best.manifest.json"
$PersonModel = Join-Path $Root "artifacts\models\coco-yolo26n\coco-yolo26n.onnx"
$EvidenceDir = Join-Path $Root "artifacts\evaluation"
$Timestamp = Get-Date -Format "yyyyMMddTHHmmss"
$AuditOut = Join-Path $EvidenceDir "windows-live-$Timestamp.audit.jsonl"
$PredictionOut = Join-Path $EvidenceDir "windows-live-$Timestamp.predictions.jsonl"

foreach ($Path in @($Executable, $FireModel, $FireManifest, $PersonModel)) {
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Required local runtime path is missing: $Path"
    }
}

if ($Provider -eq "CUDA") {
    if (-not (Test-Path -LiteralPath $TorchLib)) {
        throw "CUDA runtime support path is missing: $TorchLib"
    }
    $env:PATH = "$TorchLib;$env:PATH"
}

New-Item -ItemType Directory -Force -Path $EvidenceDir | Out-Null

$Arguments = @(
    "live-camera",
    (Join-Path $Root "configs\missions\fire_patrol.demo.json"),
    "--source", $Source,
    "--onnx-model", $FireModel,
    "--model-manifest", $FireManifest,
    "--class-names", "flame,smoke",
    "--safety-onnx-model", $PersonModel,
    "--safety-model-coco80",
    "--output-coordinates", "letterbox_xyxy_px",
    "--confidence-threshold", "0.10",
    "--flame-confidence-threshold", "0.72",
    "--smoke-confidence-threshold", "0.60",
    "--candidate-stability-frames", "6",
    "--person-veto-fire-coverage", "0.40",
    "--capture-queue-frames", "4",
    "--audit-out", $AuditOut,
    "--prediction-log-out", $PredictionOut
)

if ($Provider -eq "CPU") {
    $Arguments += @("--provider", "CPUExecutionProvider")
}
elseif ($Provider -eq "CUDA") {
    $Arguments += @(
        "--provider", "CUDAExecutionProvider",
        "--provider", "CPUExecutionProvider"
    )
}

if ($MaxFrames -gt 0) {
    $Arguments += @("--max-frames", $MaxFrames.ToString())
}
if ($NoDisplay) {
    $Arguments += "--no-display"
}

Write-Host "Starting Windows fire patrol recognition ($Provider provider)."
Write-Host "Audit: $AuditOut"
Write-Host "Predictions: $PredictionOut"
& $Executable @Arguments
exit $LASTEXITCODE
