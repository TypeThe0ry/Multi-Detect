[CmdletBinding()]
param(
    [ValidateSet('Debug', 'Release', 'RelWithDebInfo')]
    [string]$Configuration = 'Release',

    [string]$Version = '',

    [ValidateRange(1, 64)]
    [int]$Parallel = 2,

    [switch]$SkipConfigure,
    [switch]$BuildSelfTests,
    [switch]$InstallPortable,

    [string]$ReleaseGateManifest = '',
    [switch]$RequireReleaseGate,

    [string]$PortablePath = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# A nested Windows PowerShell host can omit the automatic LASTEXITCODE
# variable when a native command emits no output.  Keep build failures
# visible without allowing that host quirk to abort a successful build under
# StrictMode.
function Get-NativeExitCode {
    $lastExitCodeVariable = Get-Variable -Name LASTEXITCODE -ErrorAction SilentlyContinue
    if ($null -eq $lastExitCodeVariable) {
        return 0
    }
    try {
        return [int]$lastExitCodeVariable.Value
    }
    catch {
        return 1
    }
}

# Keep the build wrapper independent of PowerShell module auto-loading.  Some
# nested Windows PowerShell hosts do not expose the optional hash cmdlet even
# though the build itself completed successfully, so hash the executable
# through framework APIs available in both Windows PowerShell 5.1 and pwsh.
function Get-Sha256Hex {
    param([Parameter(Mandatory)] [string]$Path)

    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        return ([System.BitConverter]::ToString($sha256.ComputeHash($stream))).Replace('-', '')
    }
    finally {
        $stream.Dispose()
        $sha256.Dispose()
    }
}

function Find-FirstExisting {
    param([string[]]$Candidates)
    foreach ($candidate in $Candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    return $null
}

function UserOrProcessEnvironment {
    param([string]$Name)
    $userValue = [Environment]::GetEnvironmentVariable($Name, 'User')
    if ($userValue) {
        return $userValue
    }
    return [Environment]::GetEnvironmentVariable($Name, 'Process')
}

function Read-ProductVersionManifest {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Product version manifest not found: $Path"
    }
    try {
        $manifest = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        throw "Product version manifest is not valid JSON: $Path ($($_.Exception.Message))"
    }

    $expectedFields = @('schema_version', 'product', 'release_label', 'numeric', 'python', 'channel')
    $extraFields = @($manifest.PSObject.Properties.Name | Where-Object { $_ -notin $expectedFields })
    if ($extraFields.Count -gt 0) {
        throw "Product version manifest contains unsupported field(s): $($extraFields -join ', ')"
    }
    $missingFields = @($expectedFields | Where-Object { $_ -notin $manifest.PSObject.Properties.Name })
    if ($missingFields.Count -gt 0) {
        throw "Product version manifest is missing field(s): $($missingFields -join ', '): $Path"
    }
    if ($manifest.schema_version -isnot [int] -and $manifest.schema_version -isnot [long]) {
        throw "Product version manifest schema_version must be an integer: $Path"
    }
    foreach ($field in @('product', 'release_label', 'numeric', 'python', 'channel')) {
        if ($manifest.$field -isnot [string] -or [string]::IsNullOrWhiteSpace($manifest.$field)) {
            throw "Product version manifest '$field' must be a non-empty string: $Path"
        }
        if ($manifest.$field -cne $manifest.$field.Trim()) {
            throw "Product version manifest '$field' must not contain surrounding whitespace: $Path"
        }
    }
    if ($manifest.schema_version -ne 1) {
        throw "Unsupported product version manifest schema: $($manifest.schema_version)"
    }
    if ([string]$manifest.product -ne 'Multi-Detect') {
        throw "Unexpected product in version manifest: $($manifest.product)"
    }

    $numeric = [string]$manifest.numeric
    $releaseLabel = [string]$manifest.release_label
    $pythonVersion = [string]$manifest.python
    $channel = [string]$manifest.channel
    if ($numeric -notmatch '^\d+\.\d+\.\d+$') {
        throw "Invalid numeric product version: $numeric"
    }
    $escapedNumeric = [Regex]::Escape($numeric)
    if ($channel -eq 'prerelease') {
        if ($releaseLabel -notmatch "^$escapedNumeric-pre\.[1-9]\d*$") {
            throw "Prerelease label does not match numeric version: $releaseLabel"
        }
        if ($pythonVersion -notmatch "^${escapedNumeric}rc[1-9]\d*$") {
            throw "Python prerelease version does not match numeric version: $pythonVersion"
        }
        $labelSequence = [Regex]::Match($releaseLabel, '-pre\.(\d+)$').Groups[1].Value
        $pythonSequence = [Regex]::Match($pythonVersion, 'rc(\d+)$').Groups[1].Value
        if ($labelSequence -ne $pythonSequence) {
            throw "Prerelease sequences differ: release=$labelSequence python=$pythonSequence"
        }
    } elseif ($channel -eq 'release') {
        if ($releaseLabel -ne $numeric -or $pythonVersion -ne $numeric) {
            throw 'Release channel requires release_label and python to equal numeric'
        }
    } else {
        throw "Unsupported product release channel: $channel"
    }

    return [PSCustomObject]@{
        ReleaseLabel = $releaseLabel
        Numeric = $numeric
        Python = $pythonVersion
        Channel = $channel
    }
}

function Get-CMakeCacheValue {
    param(
        [string]$CachePath,
        [string]$Name
    )

    $match = Select-String -LiteralPath $CachePath -Pattern "^$([Regex]::Escape($Name)):[^=]*=(.*)$" |
        Select-Object -First 1
    if ($null -eq $match) {
        return $null
    }
    return $match.Matches[0].Groups[1].Value
}

function Assert-CMakeCacheValue {
    param(
        [string]$CachePath,
        [string]$Name,
        [string]$Expected,
        [switch]$PathValue
    )

    $actual = Get-CMakeCacheValue -CachePath $CachePath -Name $Name
    if ($null -eq $actual) {
        throw "-SkipConfigure rejected: CMake cache is missing $Name ($CachePath)"
    }
    if ($PathValue) {
        $trimCharacters = [char[]]@('\', '/')
        $actual = [IO.Path]::GetFullPath($actual).TrimEnd($trimCharacters).Replace('\', '/')
        $expectedValue = [IO.Path]::GetFullPath($Expected).TrimEnd($trimCharacters).Replace('\', '/')
        if (-not $actual.Equals($expectedValue, [StringComparison]::OrdinalIgnoreCase)) {
            throw "-SkipConfigure rejected: $Name='$actual', expected '$expectedValue'"
        }
    } elseif ($actual -cne $Expected) {
        throw "-SkipConfigure rejected: $Name='$actual', expected '$Expected'"
    }
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$versionManifestPath = Join-Path $repoRoot 'configs\product-version.json'
$productVersion = Read-ProductVersionManifest -Path $versionManifestPath
if ($Version -and $Version -ne $productVersion.ReleaseLabel) {
    throw (
        "-Version '$Version' does not match the canonical release label " +
        "'$($productVersion.ReleaseLabel)' in $versionManifestPath"
    )
}
$Version = $productVersion.ReleaseLabel
$numericVersion = $productVersion.Numeric
$sourceDir = Join-Path $repoRoot 'ground-control\MultiDetectGCS'
$buildDir = Join-Path $repoRoot ("build\windows-v{0}-{1}" -f $Version, $Configuration.ToLowerInvariant())
$releaseGateScript = Join-Path $repoRoot 'scripts\check_release_gate.py'
$releaseGateReport = Join-Path $buildDir 'release-gate-report.json'
$cpmCache = Join-Path $sourceDir '.cache\CPM'
$venvDir = Join-Path $sourceDir '.venv\Scripts'
$pythonExe = Join-Path $venvDir 'python.exe'

if (-not (Test-Path -LiteralPath $sourceDir)) {
    throw "MultiDetectGCS source directory not found: $sourceDir"
}
if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw "Python venv missing: $pythonExe. Run: $sourceDir\.venv\Scripts\python.exe tools/setup/install_python.py all"
}

$releaseGateRequired = $RequireReleaseGate -or ($productVersion.Channel -eq 'release')
if ($releaseGateRequired -and [string]::IsNullOrWhiteSpace($ReleaseGateManifest)) {
    throw (
        "Release channel/build requires a four-mode E7 manifest. Pass " +
        "-ReleaseGateManifest <path> (and keep the manifest hash-bound)"
    )
}
if (-not [string]::IsNullOrWhiteSpace($ReleaseGateManifest)) {
    if (-not (Test-Path -LiteralPath $ReleaseGateManifest -PathType Leaf)) {
        throw "Release-gate manifest not found: $ReleaseGateManifest"
    }
    if (-not (Test-Path -LiteralPath $releaseGateScript -PathType Leaf)) {
        throw "Release-gate evaluator is missing: $releaseGateScript"
    }
    $oldPythonPath = $env:PYTHONPATH
    try {
        $env:PYTHONPATH = Join-Path $repoRoot 'src'
        & $pythonExe $releaseGateScript $ReleaseGateManifest `
            '--version-manifest' $versionManifestPath `
            '--out' $releaseGateReport `
            '--report-only'
        $releaseGateExitCode = Get-NativeExitCode
        if ($releaseGateExitCode -ne 0) {
            throw "Release-gate evaluator failed with exit code $releaseGateExitCode"
        }
    }
    finally {
        $env:PYTHONPATH = $oldPythonPath
    }
    if (-not (Test-Path -LiteralPath $releaseGateReport -PathType Leaf)) {
        throw "Release-gate evaluator did not write its report: $releaseGateReport"
    }
    try {
        $releaseGateResult = Get-Content -LiteralPath $releaseGateReport -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        throw "Release-gate report is not valid JSON: $releaseGateReport ($($_.Exception.Message))"
    }
    if ([bool]$releaseGateResult.gate_passed -ne $true) {
        $reasons = @($releaseGateResult.release_blocked_reasons) -join '; '
        throw "E7 release-gate evidence is incomplete: $reasons"
    }
    if ($productVersion.Channel -eq 'release' -and [bool]$releaseGateResult.release_allowed -ne $true) {
        $reasons = @($releaseGateResult.release_blocked_reasons) -join '; '
        throw "Final release is blocked by E7 release gate: $reasons"
    }
    Write-Host "Release gate: evidence passed (report $releaseGateReport)"
}

$cmakeCommand = $null
try { $cmakeCommand = (Get-Command cmake -ErrorAction Stop).Source } catch { }
$cmake = Find-FirstExisting @($cmakeCommand, 'C:\Program Files\CMake\bin\cmake.exe')
$gitCommand = $null
try { $gitCommand = (Get-Command git -ErrorAction Stop).Source } catch { }
$git = Find-FirstExisting @($gitCommand, 'C:\Program Files\Git\cmd\git.exe')
$patchCommand = $null
try { $patchCommand = (Get-Command patch -ErrorAction Stop).Source } catch { }
$gitInstallRoot = if ($git) {
    Split-Path -Parent (Split-Path -Parent $git)
} else {
    $null
}
$patchExe = Find-FirstExisting @(
    $patchCommand,
    $(if ($gitInstallRoot) { Join-Path $gitInstallRoot 'usr\bin\patch.exe' } else { $null }),
    'C:\Program Files\Git\usr\bin\patch.exe',
    'C:\msys64\usr\bin\patch.exe'
)
$qtRoot = UserOrProcessEnvironment 'QT_ROOT_DIR'
if (-not $qtRoot -or -not (Test-Path -LiteralPath $qtRoot)) {
    # Keep the checkout portable across the laptop/desktop accounts that use
    # it.  Do not bake one operator's profile name into the build recipe;
    # discover sibling-user Qt installs only as a fallback after the current
    # user's path and the conventional C:\Qt location.
    $qtCandidates = @(
        "$env:USERPROFILE\Qt\6.11.1\msvc2022_64",
        'C:\Qt\6.11.1\msvc2022_64'
    )
    $userProfilesRoot = 'C:\Users'
    if (Test-Path -LiteralPath $userProfilesRoot -PathType Container) {
        $qtCandidates += @(
            Get-ChildItem -LiteralPath $userProfilesRoot -Directory -ErrorAction SilentlyContinue |
                ForEach-Object { Join-Path $_.FullName 'Qt\6.11.1\msvc2022_64' }
        )
    }
    $qtRoot = Find-FirstExisting $qtCandidates
}
$qtCmake = if ($qtRoot) { Join-Path $qtRoot 'bin\qt-cmake.bat' } else { $null }
$gstRoot = UserOrProcessEnvironment 'GStreamer_ROOT_DIR'
if (-not $gstRoot -or -not (Test-Path -LiteralPath $gstRoot)) {
    $gstRoot = UserOrProcessEnvironment 'GSTREAMER_1_0_ROOT_MSVC_X86_64'
}
if (-not $gstRoot -or -not (Test-Path -LiteralPath $gstRoot)) {
    $gstRoot = Join-Path $cpmCache 'gstreamer-win-x86_64-1.28.4\sdk'
}
$vsDevCmd = Find-FirstExisting @(
    'C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\Tools\VsDevCmd.bat',
    'C:\Program Files\Microsoft Visual Studio\2022\Community\Common7\Tools\VsDevCmd.bat',
    'C:\Program Files (x86)\Microsoft Visual Studio\2022\Community\Common7\Tools\VsDevCmd.bat'
)

foreach ($required in @{
        CMake = $cmake
        Git = $git
        Patch = $patchExe
        Qt = $qtCmake
        VisualStudio = $vsDevCmd
    }.GetEnumerator()) {
    if (-not $required.Value) {
        throw "$($required.Key) was not found. Install the Windows build prerequisites or set the corresponding environment variable."
    }
}
if (-not (Test-Path -LiteralPath (Join-Path $gstRoot 'bin\gstreamer-1.0-0.dll'))) {
    throw "GStreamer SDK is incomplete: $gstRoot"
}

$cmakeDir = Split-Path -Parent $cmake
$gitDir = Split-Path -Parent $git
$portableDir = if ($PortablePath) {
    [IO.Path]::GetFullPath((Join-Path (Get-Location) $PortablePath))
} else {
    Join-Path $buildDir ("portable-v{0}" -f $Version)
}

Write-Host "Repository : $repoRoot"
Write-Host "Version    : $Version ($($productVersion.Channel); numeric $numericVersion; Python $($productVersion.Python))"
Write-Host "Source     : $sourceDir"
Write-Host "Build      : $buildDir"
Write-Host "Qt         : $qtRoot"
Write-Host "GStreamer  : $gstRoot"
Write-Host "Patch      : $patchExe"
Write-Host "MSVC shell : $vsDevCmd"
Write-Host "Parallel   : $Parallel"

$cmdFile = Join-Path $env:TEMP ("multidetect-build-{0}.cmd" -f $PID)
$configureBlock = if ($SkipConfigure) {
    $cachePath = Join-Path $buildDir 'CMakeCache.txt'
    if (-not (Test-Path -LiteralPath $cachePath -PathType Leaf)) {
        throw "-SkipConfigure rejected: CMake cache not found: $cachePath"
    }
    Assert-CMakeCacheValue -CachePath $cachePath -Name 'CMAKE_HOME_DIRECTORY' -Expected $sourceDir -PathValue
    Assert-CMakeCacheValue -CachePath $cachePath -Name 'CMAKE_BUILD_TYPE' -Expected $Configuration
    Assert-CMakeCacheValue -CachePath $cachePath -Name 'QGC_APP_VERSION_OVERRIDE' -Expected $numericVersion
    Assert-CMakeCacheValue -CachePath $cachePath -Name 'QGC_APP_RELEASE_LABEL' -Expected $Version
    Assert-CMakeCacheValue -CachePath $cachePath -Name 'MULTIDETECT_PRODUCT_VERSION_MANIFEST' -Expected $versionManifestPath -PathValue
    Assert-CMakeCacheValue -CachePath $cachePath -Name 'PATCH_EXECUTABLE' -Expected $patchExe -PathValue
    "rem configure skipped by -SkipConfigure"
} else {
    @"
call "$qtCmake" -S "$sourceDir" -B "$buildDir" -G Ninja -DCMAKE_BUILD_TYPE=$Configuration "-DCMAKE_INSTALL_PREFIX=$buildDir\staging" "-DMULTIDETECT_PRODUCT_VERSION_MANIFEST=$versionManifestPath" "-DQGC_APP_VERSION_OVERRIDE=$numericVersion" "-DQGC_APP_RELEASE_LABEL=$Version" "-DQGC_BUILD_INSTALLER=OFF" "-DGIT_EXECUTABLE=$git" "-DPATCH_EXECUTABLE=$patchExe" "-DCMAKE_ASM_COMPILER=ml64.exe"
if errorlevel 1 exit /b %errorlevel%
"@
}
$buildTargets = @('MultiDetectGCS', 'MultiDetectReplayRtspPublisher')
if ($BuildSelfTests) {
    $buildTargets += @(
        'MultiDetectOperatorProtocolSelfTest',
        'MultiDetectTargetEvidenceStoreSelfTest',
        'MultiDetectProducerEpochDiagnosticsSelfTest',
        'MultiDetectDepthGridProtocolSelfTest',
        'MultiDetectReplayRtspWireSelfTest'
    )
}
$buildTargetArguments = $buildTargets -join ' '
$installBlock = if ($InstallPortable) {
    @"
"$cmake" --install "$buildDir" --config $Configuration --prefix "$portableDir"
if errorlevel 1 exit /b %errorlevel%
echo PORTABLE_OUTPUT=$portableDir
"@
} else {
    'rem portable install skipped; pass -InstallPortable to deploy Qt/GStreamer runtime files'
}

$cmdContents = @"
@echo off
setlocal
call "$vsDevCmd" -arch=amd64 >nul
if errorlevel 1 exit /b %errorlevel%
set "PATH=$gitDir;$cmakeDir;$venvDir;$qtRoot\bin;$gstRoot\bin;%PATH%"
set "QT_ROOT_DIR=$qtRoot"
set "GStreamer_ROOT_DIR=$gstRoot"
set "GSTREAMER_1_0_ROOT_MSVC_X86_64=$gstRoot"
set "GSTREAMER_1_0_ROOT_X86_64=$gstRoot"
set "CPM_SOURCE_CACHE=$cpmCache"
$configureBlock
"$cmake" --build "$buildDir" --config $Configuration --target $buildTargetArguments --parallel $Parallel
if errorlevel 1 exit /b %errorlevel%
$installBlock
echo BUILD_OUTPUT=$buildDir\$Configuration\MultiDetectGCS.exe
exit /b 0
"@

try {
    [IO.File]::WriteAllText($cmdFile, $cmdContents, [Text.Encoding]::ASCII)
    & cmd.exe /d /s /c "`"$cmdFile`""
    $exitCode = Get-NativeExitCode
} finally {
    if ([IO.File]::Exists($cmdFile)) {
        [IO.File]::Delete($cmdFile)
    }
}

if ($exitCode -ne 0) {
    throw "MultiDetectGCS build failed with exit code $exitCode"
}

$releaseExe = Join-Path $buildDir "$Configuration\MultiDetectGCS.exe"
if (-not (Test-Path -LiteralPath $releaseExe)) {
    throw "Build returned success but the executable is missing: $releaseExe"
}
$hash = Get-Sha256Hex -Path $releaseExe
Write-Host "Release EXE : $releaseExe"
Write-Host "SHA-256     : $hash"
if ($InstallPortable) {
    $portableExe = Join-Path $portableDir 'bin\MultiDetectGCS.exe'
    if (-not (Test-Path -LiteralPath $portableExe)) {
        throw "Install returned success but the portable executable is missing: $portableExe"
    }
    $portableReplayRtspHelper = Join-Path $portableDir 'bin\MultiDetectReplayRtspPublisher.exe'
    if (-not (Test-Path -LiteralPath $portableReplayRtspHelper)) {
        throw "Install returned success but the Replay RTSP helper is missing: $portableReplayRtspHelper"
    }
    Write-Host "Portable EXE: $portableExe"
    Write-Host "Replay RTSP : $portableReplayRtspHelper"
}
