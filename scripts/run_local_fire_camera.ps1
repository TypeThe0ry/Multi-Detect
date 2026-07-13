param(
    [string]$Source = "0"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$TorchLib = Join-Path $Root ".venv-train\Lib\site-packages\torch\lib"
$Executable = Join-Path $Root ".venv\Scripts\multi-detect.exe"
$FireModel = Join-Path $Root "artifacts\training\hardneg-snapshots\v5-local-calibrated\best.onnx"
$FireManifest = Join-Path $Root "artifacts\training\hardneg-snapshots\v5-local-calibrated\best.manifest.json"
$PersonModel = Join-Path $Root "artifacts\models\coco-yolo26n\coco-yolo26n.onnx"

foreach ($Path in @($TorchLib, $Executable, $FireModel, $FireManifest, $PersonModel)) {
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Required local runtime path is missing: $Path"
    }
}

$env:PATH = "$TorchLib;$env:PATH"
& $Executable live-camera `
    (Join-Path $Root "configs\missions\fire_patrol.demo.json") `
    --source $Source `
    --onnx-model $FireModel `
    --model-manifest $FireManifest `
    --class-names flame,smoke `
    --safety-onnx-model $PersonModel `
    --safety-model-coco80 `
    --output-coordinates letterbox_xyxy_px `
    --confidence-threshold 0.10 `
    --flame-confidence-threshold 0.72 `
    --smoke-confidence-threshold 0.60 `
    --candidate-stability-frames 6 `
    --person-veto-fire-coverage 0.40 `
    --provider CUDAExecutionProvider
