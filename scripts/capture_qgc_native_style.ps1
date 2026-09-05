[CmdletBinding()]
param(
    [string]$ExecutablePath = '',
    [string]$OutputDirectory = '',
    [ValidateRange(500, 30000)]
    [int]$SettleMilliseconds = 3500,
    [ValidateRange(-1, 16)]
    [int]$PreferMonitorIndex = -1,
    [ValidateRange(1024, 7680)]
    [int]$CaptureWidth = 1600,
    [ValidateRange(640, 4320)]
    [int]$CaptureHeight = 900,
    [switch]$UseSoftwareOpenGL,
    [switch]$KeepProcess,
    [ValidateRange(0, 2147483647)]
    [int]$ExistingProcessId = 0,
    # Some system windows (notably Task Manager) ignore SW_MINIMIZE while
    # staying above the capture surface.  This opt-in fallback hides only
    # those stubborn occluders and restores their placement in finally.
    [switch]$HideStubbornOccluders,
    # Records explicit operator permission separately from whether the host
    # actually exposes the @oai/sky Computer Use client.  This switch never
    # upgrades a machine screenshot into human visual acceptance.
    [switch]$ComputerUseOperatorAuthorized
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Resolve-ExistingFile {
    param([string]$Path)
    if (-not $Path) { return $null }
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    return (Resolve-Path -LiteralPath $Path).Path
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$attachedProcess = $ExistingProcessId -gt 0
$attachedQgcProcess = $null
if (-not $attachedProcess -and -not $ExecutablePath) {
    $candidate = Get-ChildItem -LiteralPath (Join-Path $repoRoot 'build') -Directory -Filter 'windows-v*-release' |
        Sort-Object LastWriteTime -Descending |
        ForEach-Object {
            $portable = Get-ChildItem -LiteralPath $_.FullName -Directory -Filter 'portable-v*' -ErrorAction SilentlyContinue |
                Sort-Object LastWriteTime -Descending | Select-Object -First 1
            if ($portable) {
                Resolve-ExistingFile (Join-Path $portable.FullName 'bin\MultiDetectGCS.exe')
            }
        } |
        Where-Object { $_ } |
        Select-Object -First 1
    $ExecutablePath = $candidate
}
if ($attachedProcess) {
    $attachedQgcProcess = Get-Process -Id $ExistingProcessId -ErrorAction Stop
    if ($attachedQgcProcess.ProcessName -cne 'MultiDetectGCS') {
        throw "Existing process $ExistingProcessId is '$($attachedQgcProcess.ProcessName)', not MultiDetectGCS."
    }
    $ExecutablePath = Resolve-ExistingFile $attachedQgcProcess.Path
    if (-not $ExecutablePath) {
        throw "Unable to resolve the executable path for MultiDetectGCS process $ExistingProcessId."
    }
}
else {
    $ExecutablePath = Resolve-ExistingFile $ExecutablePath
    if (-not $ExecutablePath) {
        throw 'MultiDetectGCS.exe was not found. Pass -ExecutablePath <portable>\bin\MultiDetectGCS.exe.'
    }
}

if (-not $OutputDirectory) {
    $stamp = [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmssZ')
    $OutputDirectory = Join-Path $repoRoot "build\qgc-native-style-$stamp"
}
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null

if (-not ([System.Management.Automation.PSTypeName]'MultiDetectNativeStyle.Native').Type) {
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;

namespace MultiDetectNativeStyle {
    public static class Native {
        [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
        [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X, Y; }
        [StructLayout(LayoutKind.Sequential)] public struct WINDOWPLACEMENT {
            public int length, flags, showCmd;
            public POINT ptMinPosition, ptMaxPosition;
            public RECT rcNormalPosition;
        }
        public sealed class WindowInfo {
            public long Handle;
            public uint ProcessId;
            public RECT Rect;
            public string Title = "";
        }
        public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
        [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc callback, IntPtr lParam);
        [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
        [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
        [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
        [DllImport("user32.dll")] public static extern bool GetClientRect(IntPtr hWnd, out RECT rect);
        [DllImport("user32.dll")] public static extern bool ClientToScreen(IntPtr hWnd, ref POINT point);
        [DllImport("user32.dll")] public static extern bool GetWindowPlacement(IntPtr hWnd, ref WINDOWPLACEMENT placement);
        [DllImport("user32.dll")] public static extern bool SetWindowPlacement(IntPtr hWnd, ref WINDOWPLACEMENT placement);
        [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr hWnd, int command);
        [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr insertAfter, int x, int y, int cx, int cy, uint flags);
        [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
        [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
        [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
        [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr hWnd);
        [DllImport("user32.dll", EntryPoint = "GetWindowLongPtrW")] public static extern IntPtr GetWindowLongPtr(IntPtr hWnd, int index);
        [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int count);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetClassName(IntPtr hWnd, StringBuilder text, int count);

        public static WindowInfo[] VisibleWindows() {
            var windows = new List<WindowInfo>();
            EnumWindows((candidate, unused) => {
                if (!IsWindowVisible(candidate) || IsIconic(candidate)) return true;
                RECT rect;
                if (!GetWindowRect(candidate, out rect)) return true;
                uint processId;
                GetWindowThreadProcessId(candidate, out processId);
                var title = new StringBuilder(512);
                GetWindowText(candidate, title, title.Capacity);
                windows.Add(new WindowInfo {
                    Handle = candidate.ToInt64(),
                    ProcessId = processId,
                    Rect = rect,
                    Title = title.ToString()
                });
                return true;
            }, IntPtr.Zero);
            return windows.ToArray();
        }

        public static string ClassName(IntPtr hWnd) {
            var text = new StringBuilder(256);
            GetClassName(hWnd, text, text.Capacity);
            return text.ToString();
        }
    }
}
'@
}

function Test-RectIntersection {
    param($First, $Second)
    return $First.Left -lt $Second.Right -and $First.Right -gt $Second.Left -and
        $First.Top -lt $Second.Bottom -and $First.Bottom -gt $Second.Top
}

function Get-ClientScreenRect {
    param([IntPtr]$Handle)
    [MultiDetectNativeStyle.Native+RECT]$client = New-Object MultiDetectNativeStyle.Native+RECT
    if (-not [MultiDetectNativeStyle.Native]::GetClientRect($Handle, [ref]$client)) {
        throw 'GetClientRect failed for QGC main window.'
    }
    [MultiDetectNativeStyle.Native+POINT]$topLeft = New-Object MultiDetectNativeStyle.Native+POINT
    $topLeft.X = $client.Left
    $topLeft.Y = $client.Top
    [MultiDetectNativeStyle.Native+POINT]$bottomRight = New-Object MultiDetectNativeStyle.Native+POINT
    $bottomRight.X = $client.Right
    $bottomRight.Y = $client.Bottom
    if (-not [MultiDetectNativeStyle.Native]::ClientToScreen($Handle, [ref]$topLeft) -or
        -not [MultiDetectNativeStyle.Native]::ClientToScreen($Handle, [ref]$bottomRight)) {
        throw 'ClientToScreen failed for QGC main window.'
    }
    return [MultiDetectNativeStyle.Native+RECT]@{
        Left = $topLeft.X; Top = $topLeft.Y; Right = $bottomRight.X; Bottom = $bottomRight.Y
    }
}

function Get-WindowTitle {
    param([IntPtr]$Handle)
    $text = New-Object Text.StringBuilder 512
    $null = [MultiDetectNativeStyle.Native]::GetWindowText($Handle, $text, $text.Capacity)
    return $text.ToString()
}

function Get-IntersectingWindows {
    param([IntPtr]$QgcHandle, [uint32]$QgcPid, $CaptureRect)
    $matches = @()
    foreach ($window in [MultiDetectNativeStyle.Native]::VisibleWindows()) {
        $handle = [IntPtr]$window.Handle
        if ($handle -eq $QgcHandle -or $window.ProcessId -eq $QgcPid) { continue }
        $class = [MultiDetectNativeStyle.Native]::ClassName($handle)
        if ($class -in @('Shell_TrayWnd', 'Progman', 'WorkerW', 'Windows.UI.Core.CoreWindow')) { continue }
        if (Test-RectIntersection $window.Rect $CaptureRect) {
            $matches += [pscustomobject]@{
                Handle = $handle; ProcessId = [uint32]$window.ProcessId; Title = $window.Title; ClassName = $class
            }
        }
    }
    return $matches
}

function Wait-QgcWindow {
    param([Diagnostics.Process]$Process)
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($Process.HasExited) { throw 'QGC exited before exposing its main window.' }
        $Process.Refresh()
        if ($Process.MainWindowHandle -ne [IntPtr]::Zero) { return $Process.MainWindowHandle }
        Start-Sleep -Milliseconds 100
    }
    throw 'QGC main window was not available within 30 seconds.'
}

function Convert-Rect {
    param($Rect)
    return [ordered]@{ left = $Rect.Left; top = $Rect.Top; right = $Rect.Right; bottom = $Rect.Bottom; width = $Rect.Right - $Rect.Left; height = $Rect.Bottom - $Rect.Top }
}

function Get-MonitorPlacement {
    param(
        [IntPtr]$QgcHandle,
        [uint32]$QgcPid,
        [int]$WindowWidth,
        [int]$WindowHeight
    )
    Add-Type -AssemblyName System.Windows.Forms
    $screens = @([System.Windows.Forms.Screen]::AllScreens)
    if ($screens.Count -eq 0) { throw 'No desktop monitor was returned by Windows.' }
    $candidates = @()
    for ($index = 0; $index -lt $screens.Count; $index++) {
        if ($PreferMonitorIndex -ge 0 -and $index -ne $PreferMonitorIndex) { continue }
        $working = $screens[$index].WorkingArea
        if ($working.Width -lt $WindowWidth -or $working.Height -lt $WindowHeight) { continue }
        $candidateRect = [pscustomobject]@{
            Left = $working.Left; Top = $working.Top
            Right = $working.Left + $WindowWidth; Bottom = $working.Top + $WindowHeight
        }
        $intersections = @(Get-IntersectingWindows -QgcHandle $QgcHandle -QgcPid $QgcPid -CaptureRect $candidateRect)
        $candidates += [pscustomobject]@{
            Index = $index; Left = $working.Left; Top = $working.Top
            Width = $WindowWidth; Height = $WindowHeight
            OccluderCount = $intersections.Count; Area = $working.Width * $working.Height
        }
    }
    if ($candidates.Count -eq 0) { throw "No monitor can fit the QGC window (${WindowWidth}x${WindowHeight})." }
    return $candidates | Sort-Object OccluderCount, @{ Expression = 'Area'; Descending = $true } | Select-Object -First 1
}

$stdoutPath = Join-Path $OutputDirectory 'qgc.stdout.log'
$stderrPath = Join-Path $OutputDirectory 'qgc.stderr.log'
$capturePath = Join-Path $OutputDirectory 'qgc-native-style.png'
$observationPath = Join-Path $OutputDirectory 'process-observation.json'
$arguments = @('--offline', '--allow-multiple', '--log-output')
if ($UseSoftwareOpenGL) { $arguments += '--swrast' }

$process = $attachedQgcProcess
$qgcHandle = [IntPtr]::Zero
$originalPlacement = $null
$originalTopmost = $false
$occluderPlacements = @()
$hiddenOccluders = @()
$captureError = $null
$captureRect = $null
$occludersBefore = @()
$occludersAfter = @()
$captureSucceeded = $false
$qgcTitle = $null
$monitorPlacement = $null
$previousForeground = [IntPtr]::Zero
$startedAt = [DateTime]::UtcNow

try {
    if (-not $attachedProcess) {
        $process = Start-Process -FilePath $ExecutablePath -ArgumentList $arguments -WorkingDirectory (Split-Path -Parent $ExecutablePath) `
            -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -PassThru
    }
    $qgcHandle = Wait-QgcWindow -Process $process
    $qgcTitle = Get-WindowTitle $qgcHandle
    Start-Sleep -Milliseconds $SettleMilliseconds

    [MultiDetectNativeStyle.Native+WINDOWPLACEMENT]$placement = New-Object MultiDetectNativeStyle.Native+WINDOWPLACEMENT
    $placement.length = [Runtime.InteropServices.Marshal]::SizeOf([type][MultiDetectNativeStyle.Native+WINDOWPLACEMENT])
    if (-not [MultiDetectNativeStyle.Native]::GetWindowPlacement($qgcHandle, [ref]$placement)) {
        throw 'Unable to preserve QGC WINDOWPLACEMENT.'
    }
    $originalPlacement = $placement
    $originalTopmost = (([MultiDetectNativeStyle.Native]::GetWindowLongPtr($qgcHandle, -20).ToInt64() -band 0x8) -ne 0)
    $previousForeground = [MultiDetectNativeStyle.Native]::GetForegroundWindow()

    # Raise one QGC surface, then remove only intersecting external windows from
    # the capture rectangle. Every placement is restored in finally.
    $flags = [uint32](0x0001 -bor 0x0002 -bor 0x0040) # NOSIZE | NOMOVE | SHOWWINDOW
    $null = [MultiDetectNativeStyle.Native]::ShowWindowAsync($qgcHandle, 5)
    $null = [MultiDetectNativeStyle.Native]::SetWindowPos($qgcHandle, [IntPtr](-1), 0, 0, 0, 0, $flags)
    $null = [MultiDetectNativeStyle.Native]::BringWindowToTop($qgcHandle)
    $null = [MultiDetectNativeStyle.Native]::SetForegroundWindow($qgcHandle)
    Start-Sleep -Milliseconds 150

    [MultiDetectNativeStyle.Native+RECT]$windowRect = New-Object MultiDetectNativeStyle.Native+RECT
    if (-not [MultiDetectNativeStyle.Native]::GetWindowRect($qgcHandle, [ref]$windowRect)) {
        throw 'GetWindowRect failed before monitor selection.'
    }
    $requestedWindowWidth = [Math]::Max(($windowRect.Right - $windowRect.Left), $CaptureWidth)
    $requestedWindowHeight = [Math]::Max(($windowRect.Bottom - $windowRect.Top), $CaptureHeight)
    $monitorPlacement = Get-MonitorPlacement -QgcHandle $qgcHandle -QgcPid ([uint32]$process.Id) `
        -WindowWidth $requestedWindowWidth -WindowHeight $requestedWindowHeight
    $null = [MultiDetectNativeStyle.Native]::SetWindowPos(
        $qgcHandle, [IntPtr](-1), $monitorPlacement.Left, $monitorPlacement.Top,
        $monitorPlacement.Width, $monitorPlacement.Height, [uint32](0x0040)
    ) # SHOWWINDOW; move/resize to a monitor with the fewest occluders.
    Start-Sleep -Milliseconds 250

    $captureRect = Get-ClientScreenRect -Handle $qgcHandle
    $occludersBefore = @(Get-IntersectingWindows -QgcHandle $qgcHandle -QgcPid ([uint32]$process.Id) -CaptureRect $captureRect)
    foreach ($occluder in $occludersBefore) {
        [MultiDetectNativeStyle.Native+WINDOWPLACEMENT]$occluderPlacement = New-Object MultiDetectNativeStyle.Native+WINDOWPLACEMENT
        $occluderPlacement.length = [Runtime.InteropServices.Marshal]::SizeOf([type][MultiDetectNativeStyle.Native+WINDOWPLACEMENT])
        if ([MultiDetectNativeStyle.Native]::GetWindowPlacement($occluder.Handle, [ref]$occluderPlacement)) {
            $occluderPlacements += [pscustomobject]@{
                Handle = $occluder.Handle
                Placement = $occluderPlacement
                Title = $occluder.Title
                ClassName = $occluder.ClassName
                Hidden = $false
            }
            $null = [MultiDetectNativeStyle.Native]::ShowWindowAsync($occluder.Handle, 6) # SW_MINIMIZE
        }
    }
    Start-Sleep -Milliseconds 250
    $occludersAfter = @(Get-IntersectingWindows -QgcHandle $qgcHandle -QgcPid ([uint32]$process.Id) -CaptureRect $captureRect)
    if ($occludersAfter.Count -ne 0 -and $HideStubbornOccluders) {
        # Minimize is the least invasive operation.  Only windows that still
        # intersect and whose placement was captured are hidden as a fallback.
        foreach ($remaining in $occludersAfter) {
            $saved = $occluderPlacements | Where-Object { $_.Handle -eq $remaining.Handle } | Select-Object -First 1
            if ($saved) {
                $null = [MultiDetectNativeStyle.Native]::ShowWindowAsync($remaining.Handle, 0) # SW_HIDE
                # ShowWindowAsync is genuinely asynchronous. Mark the saved
                # placement for restoration immediately, then verify the
                # resulting visibility only after the bounded wait below.
                $saved.Hidden = $true
                $hiddenOccluders += [pscustomobject]@{
                    Handle = $remaining.Handle
                    ProcessId = [uint32]$remaining.ProcessId
                    Title = $remaining.Title
                    ClassName = $remaining.ClassName
                }
            }
        }
        Start-Sleep -Milliseconds 150
        $occludersAfter = @(Get-IntersectingWindows -QgcHandle $qgcHandle -QgcPid ([uint32]$process.Id) -CaptureRect $captureRect)
    }
    if ($occludersAfter.Count -ne 0) {
        throw ('External window(s) still intersect exact QGC client capture: ' +
            (($occludersAfter | ForEach-Object { "$($_.Title) [$($_.ClassName)]" }) -join '; '))
    }

    Add-Type -AssemblyName System.Drawing
    $width = $captureRect.Right - $captureRect.Left
    $height = $captureRect.Bottom - $captureRect.Top
    if ($width -lt 640 -or $height -lt 480) { throw "QGC client surface is too small: ${width}x${height}." }
    $bitmap = New-Object Drawing.Bitmap $width, $height, ([Drawing.Imaging.PixelFormat]::Format24bppRgb)
    try {
        $graphics = [Drawing.Graphics]::FromImage($bitmap)
        try {
            $graphics.CopyFromScreen($captureRect.Left, $captureRect.Top, 0, 0, $bitmap.Size, [Drawing.CopyPixelOperation]::SourceCopy)
        }
        finally { $graphics.Dispose() }
        $bitmap.Save($capturePath, [Drawing.Imaging.ImageFormat]::Png)
    }
    finally { $bitmap.Dispose() }
    $captureSucceeded = $true
}
catch {
    $captureError = $_.Exception.Message
}
finally {
    foreach ($saved in $occluderPlacements) {
        [MultiDetectNativeStyle.Native+WINDOWPLACEMENT]$restore = $saved.Placement
        $null = [MultiDetectNativeStyle.Native]::SetWindowPlacement($saved.Handle, [ref]$restore)
        if ($saved.Hidden) {
            $showCommand = [int]$saved.Placement.showCmd
            if ($showCommand -eq 0 -or $showCommand -eq 2) { $showCommand = 1 }
            $null = [MultiDetectNativeStyle.Native]::ShowWindowAsync($saved.Handle, $showCommand)
        }
    }
    if ($qgcHandle -ne [IntPtr]::Zero -and $originalPlacement -ne $null) {
        [MultiDetectNativeStyle.Native+WINDOWPLACEMENT]$restoreQgc = $originalPlacement
        $null = [MultiDetectNativeStyle.Native]::SetWindowPlacement($qgcHandle, [ref]$restoreQgc)
        $restoreFlags = [uint32](0x0001 -bor 0x0002 -bor 0x0010) # NOSIZE | NOMOVE | NOACTIVATE
        $restoreOrder = if ($originalTopmost) { [IntPtr](-1) } else { [IntPtr](-2) }
        $null = [MultiDetectNativeStyle.Native]::SetWindowPos($qgcHandle, $restoreOrder, 0, 0, 0, 0, $restoreFlags)
    }
    if ($previousForeground -ne [IntPtr]::Zero -and
        [MultiDetectNativeStyle.Native]::IsWindow($previousForeground) -and
        $previousForeground -ne $qgcHandle) {
        $null = [MultiDetectNativeStyle.Native]::SetForegroundWindow($previousForeground)
    }
    if ($process -and -not $attachedProcess -and -not $KeepProcess -and -not $process.HasExited) {
        $process.CloseMainWindow()
        if (-not $process.WaitForExit(4000)) { $process.Kill() }
    }
}

$endedAt = [DateTime]::UtcNow
$captureHash = $null
$captureBytes = 0
if (Test-Path -LiteralPath $capturePath -PathType Leaf) {
    $captureHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $capturePath).Hash
    $captureBytes = (Get-Item -LiteralPath $capturePath).Length
}
$observation = [ordered]@{
    schema = 'multidetect.qgc-native-style-capture'
    schema_version = 1
    started_at_utc = $startedAt.ToString('o')
    finished_at_utc = $endedAt.ToString('o')
    executable = $ExecutablePath
    attached_existing_process = [bool]$attachedProcess
    requested_existing_process_id = if ($attachedProcess) { $ExistingProcessId } else { $null }
    requested_capture_width = $CaptureWidth
    requested_capture_height = $CaptureHeight
    qgc_pid = if ($process) { $process.Id } else { $null }
    qgc_window_title = $qgcTitle
    exact_client_rect_screen = if ($captureRect) { Convert-Rect $captureRect } else { $null }
    selected_monitor_index = if ($monitorPlacement) { $monitorPlacement.Index } else { $null }
    selected_monitor_occluder_count_before_minimize = if ($monitorPlacement) { $monitorPlacement.OccluderCount } else { $null }
    external_occluders_before = @($occludersBefore | ForEach-Object { [ordered]@{ title = $_.Title; class_name = $_.ClassName; pid = $_.ProcessId } })
    external_occluders_after = @($occludersAfter | ForEach-Object { [ordered]@{ title = $_.Title; class_name = $_.ClassName; pid = $_.ProcessId } })
    hide_stubborn_occluders_requested = [bool]$HideStubbornOccluders
    hidden_occluder_count = @($hiddenOccluders).Count
    hidden_occluders = @($hiddenOccluders | ForEach-Object { [ordered]@{ title = $_.Title; class_name = $_.ClassName; pid = $_.ProcessId } })
    single_native_surface_machine_check = [bool]($captureSucceeded -and $occludersAfter.Count -eq 0)
    capture = [ordered]@{ path = $capturePath; bytes = $captureBytes; sha256 = $captureHash }
    computer_use_operator_authorized = [bool]$ComputerUseOperatorAuthorized
    computer_use_tool_exposed = $false
    operator_visual_acceptance = 'not_claimed'
    qml_rendering_proved = $false
    desktop_capture_gpu_surface_proved = $false
    error = $captureError
}
$observation | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $observationPath -Encoding UTF8

if ($captureError) {
    Write-Error $captureError
    exit 2
}
Write-Output ($observation | ConvertTo-Json -Depth 8)
exit 0
