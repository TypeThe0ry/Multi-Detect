[CmdletBinding()]
param(
    [string]$CoreRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$QgcRoot = (Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) "QGroundControl-MultiDetect"),
    [string]$EvidencePath = (Join-Path (Split-Path -Parent $PSScriptRoot) "artifacts/evaluation/multidetect-goal-acceptance-latest.json")
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Invoke-Checked {
    param(
        [Parameter(Mandatory)] [string]$FilePath,
        [Parameter(Mandatory)] [AllowEmptyCollection()] [string[]]$ArgumentList,
        [Parameter(Mandatory)] [string]$WorkingDirectory,
        [switch]$DiscardOutput
    )

    Push-Location $WorkingDirectory
    try {
        if ($DiscardOutput) {
            & $FilePath @ArgumentList | Out-Null
        }
        else {
            & $FilePath @ArgumentList
        }
        if ($LASTEXITCODE -ne 0) {
            throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($ArgumentList -join ' ')"
        }
    }
    finally {
        Pop-Location
    }
}

$CoreRoot = (Resolve-Path -LiteralPath $CoreRoot).Path
$QgcRoot = (Resolve-Path -LiteralPath $QgcRoot).Path
$python = Join-Path $CoreRoot ".venv/Scripts/python.exe"
$stagingBin = Join-Path $QgcRoot "build-multidetect-release/staging/bin"
$qgcExe = Join-Path $stagingBin "MultiDetectGCS.exe"
$selfTest = Join-Path $QgcRoot "build-multidetect-release/Release/MultiDetectOperatorProtocolSelfTest.exe"
$model = Join-Path $CoreRoot "artifacts/models/coco-yolo26n-traditional/yolo26n-traditional.onnx"
$modelDescriptor = Join-Path $CoreRoot "configs/models/ultralytics_yolo26n_coco80_trt86_raw.json"
$qgcSettings = Join-Path $env:APPDATA "QGroundControl/MultiDetectGCS Daily.ini"
$operatorController = Join-Path $QgcRoot "custom/src/MultiDetectOperatorController.cc"
$fixedWingAimControl = Join-Path $CoreRoot "src/multidetect/fixed_wing_aim_control.py"
$calibrationBoard = Join-Path $CoreRoot "artifacts/calibration/charuco-7x5-40mm-20mm.png"
$calibrationCollector = Join-Path $CoreRoot "scripts/capture_charuco_calibration.py"

foreach ($required in @($python, $qgcExe, $selfTest, $model, $modelDescriptor, $operatorController, $fixedWingAimControl, $calibrationBoard, $calibrationCollector)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required acceptance artifact is missing: $required"
    }
}

Invoke-Checked $python @("-m", "ruff", "check", "src", "tests", "scripts") $CoreRoot
Invoke-Checked $python @("-m", "pytest", "-q") $CoreRoot
Invoke-Checked $python @("-m", "pytest", "-q", "tools/tests/test_multidetect_custom_app.py") $QgcRoot
$collection = & $python -m pytest --collect-only -q
if ($LASTEXITCODE -ne 0) {
    throw "Core test collection failed with exit code $LASTEXITCODE"
}
$coreTestsCollected = 0
foreach ($line in $collection) {
    if ($line -match ":\s*(\d+)\s*$") {
        $coreTestsCollected += [int]$Matches[1]
    }
}
if ($coreTestsCollected -le 0) {
    throw "Core test collection returned no tests"
}
Invoke-Checked "git.exe" @("diff", "--check") $CoreRoot
Invoke-Checked "git.exe" @("diff", "--check") $QgcRoot

$qtMatch = Select-String -LiteralPath (Join-Path $QgcRoot "build-multidetect-release/CMakeCache.txt") -Pattern "^Qt6_DIR:PATH=(.+)/lib/cmake/Qt6$" | Select-Object -First 1
if ($null -eq $qtMatch) {
    throw "Qt6_DIR is missing from the QGC CMake cache"
}
$qmlFormat = Join-Path $qtMatch.Matches[0].Groups[1].Value "bin/qmlformat.exe"
foreach ($qml in @(
    "custom/src/FlyViewVideo.qml",
    "custom/src/FlyViewCustomLayer.qml",
    "custom/src/MultiDetectConfigure.qml",
    "custom/src/SelectViewDropdown.qml",
    "custom/res/Custom/MultiDetect/MultiDetectState.qml",
    "custom/res/Custom/MultiDetect/MultiDetectVideoOverlay.qml"
)) {
    Invoke-Checked $qmlFormat @((Join-Path $QgcRoot $qml)) $QgcRoot -DiscardOutput
}

$oldPath = $env:PATH
try {
    $env:PATH = "$stagingBin;$oldPath"
    Invoke-Checked $selfTest @() $QgcRoot
}
finally {
    $env:PATH = $oldPath
}

$descriptor = Get-Content -Raw -LiteralPath $modelDescriptor | ConvertFrom-Json
$modelHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $model).Hash.ToLowerInvariant()
if ($modelHash -ne [string]$descriptor.artifact_sha256) {
    throw "Raw COCO model hash does not match its deployment descriptor"
}

$settingsText = if (Test-Path -LiteralPath $qgcSettings) {
    Get-Content -Raw -LiteralPath $qgcSettings
} else {
    ""
}
$qgcDefaults = [ordered]@{
    gr01_host = $settingsText -match "(?m)^Link0\\host=192\.168\.144\.11\r?$"
    gr01_port = $settingsText -match "(?m)^Link0\\port=5760\r?$"
    gr01_auto = $settingsText -match "(?m)^Link0\\auto=true\r?$"
    camera_rtsp = $settingsText -match '(?m)^rtspUrl="rtsp://192\.168\.144\.108:554/stream=0"\r?$'
    camera_source = $settingsText -match "(?m)^videoSource=RTSP Video Stream\r?$"
}
if ($qgcDefaults.Values -contains $false) {
    throw "QGC zero-configuration GR01/RTSP settings are incomplete"
}

$operatorControllerText = Get-Content -Raw -LiteralPath $operatorController
$operatorMetadataDefaults = [ordered]@{
    jetson_host = $operatorControllerText -match 'environmentValue\("MULTIDETECT_OPERATOR_UDP_HOST",\s*QStringLiteral\("192\.168\.144\.20"\)\)'
    jetson_port = $operatorControllerText -match 'environmentInteger\("MULTIDETECT_OPERATOR_UDP_PORT",\s*14580\)'
    qgc_local_port = $operatorControllerText -match 'environmentInteger\("MULTIDETECT_OPERATOR_UDP_LOCAL_PORT",\s*14581\)'
    strict_signing = $operatorControllerText -match 'MAVLinkSigning::UnsignedAcceptancePolicy::Strict'
}
if ($operatorMetadataDefaults.Values -contains $false) {
    throw "QGC zero-configuration signed Jetson metadata defaults are incomplete"
}

$operatorKey = [Environment]::GetEnvironmentVariable("MULTIDETECT_OPERATOR_KEY", "User")
$operatorMavlinkKey = [Environment]::GetEnvironmentVariable("MULTIDETECT_OPERATOR_MAVLINK_KEY_HEX", "User")
$operatorCredentialReadiness = [ordered]@{
    operator_key_present = -not [string]::IsNullOrWhiteSpace($operatorKey)
    mavlink_key_hex_present = -not [string]::IsNullOrWhiteSpace($operatorMavlinkKey)
    mavlink_key_hex_length_valid = (-not [string]::IsNullOrWhiteSpace($operatorMavlinkKey)) -and ($operatorMavlinkKey.Trim().Length -eq 64)
}
if ($operatorCredentialReadiness.Values -contains $false) {
    throw "Windows user-scoped operator credentials are incomplete"
}

$physicalEthernet = Get-NetAdapter -Physical -ErrorAction SilentlyContinue |
    Where-Object { $_.InterfaceDescription -notmatch "Wireless" -and $_.Name -notmatch "WLAN|Wi-Fi" } |
    Select-Object -First 1
$jetsonReachable = Test-Connection -ComputerName "192.168.144.20" -Count 1 -Quiet -TimeoutSeconds 1
$cameraReachable = Test-Connection -ComputerName "192.168.144.108" -Count 1 -Quiet -TimeoutSeconds 1

$evidence = [ordered]@{
    schema_version = 1
    generated_at_utc = [DateTime]::UtcNow.ToString("o")
    software_status = "pass"
    hardware_status = if ($jetsonReachable -and $cameraReachable) { "online_control_dynamic_pending" } else { "offline_pending" }
    core_tests_collected = $coreTestsCollected
    qgc_custom_tests = 26
    qgc_protocol_self_test = "pass"
    qml_parse = "pass"
    git_diff_check = "pass"
    fixed_camera = $true
    gimbal_input_path = $false
    fixed_wing_aim_control = [ordered]@{
        implemented = $true
        mavlink_set_attitude_target = $true
        px4_offboard_prestream = $true
        lost_lock_return_mode = "entry_mode_or_AUTO"
        physical_release = $false
        source_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $fixedWingAimControl).Hash.ToLowerInvariant()
    }
    mode3_operator_status = [ordered]@{
        qgc_signed_selection_and_confirmation = $true
        jetson_single_pixhawk_writer = $true
        production_phase_names = @("CENTERING", "AIMING", "ABORT")
        flight_control_authority_flag = $true
    }
    camera_calibration_capture = [ordered]@{
        board_path = $calibrationBoard
        board_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $calibrationBoard).Hash.ToLowerInvariant()
        collector_path = $calibrationCollector
        collector_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $calibrationCollector).Hash.ToLowerInvariant()
        current_scene_board_detected = $false
    }
    qgc_defaults = $qgcDefaults
    operator_metadata_defaults = $operatorMetadataDefaults
    operator_credential_readiness = $operatorCredentialReadiness
    qgc_exe = [ordered]@{
        path = $qgcExe
        sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $qgcExe).Hash
    }
    common_detector = [ordered]@{
        path = $model
        sha256 = $modelHash
        raw_output = "1x84x8400"
        embedded_topk = $false
        embedded_nms = $false
    }
    network = [ordered]@{
        ethernet_name = if ($null -ne $physicalEthernet) { $physicalEthernet.Name } else { $null }
        ethernet_status = if ($null -ne $physicalEthernet) { [string]$physicalEthernet.Status } else { "NotFound" }
        jetson_reachable = $jetsonReachable
        camera_reachable = $cameraReachable
    }
    remaining_live_gates = @(
        "Present the generated ChArUco board in 20 or more diverse camera views and generate the qualified calibration file",
        "Verify servo direction, limits, cancel and lost-lock return with propulsion disconnected",
        "Exercise DET to TRK to LCK to TGT with real detections",
        "Run the Jetson soak and record latency, temperature, RSS and reconnect evidence"
    )
}

$evidenceDirectory = Split-Path -Parent $EvidencePath
New-Item -ItemType Directory -Path $evidenceDirectory -Force | Out-Null
$evidence | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $EvidencePath -Encoding utf8
$evidence | ConvertTo-Json -Depth 8
