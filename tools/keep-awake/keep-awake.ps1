<#
.SYNOPSIS
  系統匣小工具：讓電腦與螢幕保持喚醒（取代 Orca 的「保持電腦處於喚醒狀態」）。

.DESCRIPTION
  兩種模式，從系統匣圖示的右鍵選單切換：
    開啟  呼叫 SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)，
          系統不睡眠、螢幕不關閉。
    關    清除要求，回到正常的電源設定。
  程式結束（或當掉）時 Windows 會自動撤銷要求，不會留下任何電源設定變更。
  上次選的模式記在 %LOCALAPPDATA%\keep-awake\mode.txt，預設為「開啟」。

.PARAMETER Install
  在「啟動」資料夾建立捷徑，登入時自動執行，並立即啟動一份。

.PARAMETER Uninstall
  移除「啟動」資料夾的捷徑（不會結束正在執行的那一份）。

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File keep-awake.ps1 -Install
#>
[CmdletBinding()]
param(
    [switch]$Install,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$shortcutPath = Join-Path ([Environment]::GetFolderPath('Startup')) 'keep-awake.lnk'
$psExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$runArgs = "-NoProfile -STA -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$PSCommandPath`""

if ($Uninstall) {
    if (Test-Path -LiteralPath $shortcutPath) {
        Remove-Item -LiteralPath $shortcutPath
        Write-Output "已移除開機啟動捷徑：$shortcutPath"
    } else {
        Write-Output "沒有開機啟動捷徑：$shortcutPath"
    }
    return
}

if ($Install) {
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($shortcutPath)
    $lnk.TargetPath = $psExe
    $lnk.Arguments = $runArgs
    $lnk.WorkingDirectory = Split-Path -Parent $PSCommandPath
    $lnk.WindowStyle = 7   # minimized
    $lnk.Description = 'keep-awake: 保持電腦與螢幕喚醒'
    $lnk.Save()
    Write-Output "已建立開機啟動捷徑：$shortcutPath"
    Start-Process -FilePath $psExe -ArgumentList $runArgs -WindowStyle Hidden
    Write-Output '已啟動 keep-awake（看系統匣）。'
    return
}

# ---- 只允許一份 ----
$mutex = New-Object System.Threading.Mutex($false, 'Local\keep-awake-tray')
if (-not $mutex.WaitOne(0)) {
    Write-Output 'keep-awake 已在執行中。'
    return
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -Namespace KeepAwake -Name Native -MemberDefinition @'
[DllImport("kernel32.dll", SetLastError = true)]
public static extern uint SetThreadExecutionState(uint esFlags);
[DllImport("user32.dll", SetLastError = true)]
public static extern bool DestroyIcon(System.IntPtr hIcon);
'@

$ES_CONTINUOUS       = [uint32]'0x80000000'
$ES_SYSTEM_REQUIRED  = [uint32]'0x00000001'
$ES_DISPLAY_REQUIRED = [uint32]'0x00000002'

$stateDir  = Join-Path $env:LOCALAPPDATA 'keep-awake'
$stateFile = Join-Path $stateDir 'mode.txt'

function Read-Mode {
    try {
        if (Test-Path -LiteralPath $stateFile) {
            $m = (Get-Content -LiteralPath $stateFile -Raw).Trim()
            if ($m -in @('on', 'off')) { return $m }
        }
    } catch { }
    return 'on'
}

function Save-Mode([string]$m) {
    try {
        if (-not (Test-Path -LiteralPath $stateDir)) { New-Item -ItemType Directory -Path $stateDir | Out-Null }
        Set-Content -LiteralPath $stateFile -Value $m -Encoding ASCII
    } catch { }
}

function New-DotIcon([System.Drawing.Color]$color) {
    $bmp = New-Object System.Drawing.Bitmap 16, 16
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.Clear([System.Drawing.Color]::Transparent)
    $brush = New-Object System.Drawing.SolidBrush $color
    $g.FillEllipse($brush, 2, 2, 12, 12)
    $pen = New-Object System.Drawing.Pen ([System.Drawing.Color]::FromArgb(200, 255, 255, 255)), 1
    $g.DrawEllipse($pen, 2, 2, 12, 12)
    $g.Dispose(); $brush.Dispose(); $pen.Dispose()
    $h = $bmp.GetHicon()
    $icon = [System.Drawing.Icon]::FromHandle($h).Clone()
    [void][KeepAwake.Native]::DestroyIcon($h)
    $bmp.Dispose()
    return $icon
}

$iconOn  = New-DotIcon ([System.Drawing.Color]::FromArgb(255, 46, 160, 67))
$iconOff = New-DotIcon ([System.Drawing.Color]::FromArgb(255, 140, 140, 140))

$tray = New-Object System.Windows.Forms.NotifyIcon
$menu = New-Object System.Windows.Forms.ContextMenuStrip
$title = $menu.Items.Add('保持電腦處於喚醒狀態')
$title.Enabled = $false
[void]$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator))
$itemOn  = $menu.Items.Add('開啟 — 系統與螢幕保持喚醒')
$itemOff = $menu.Items.Add('關 — 允許正常的系統睡眠')
[void]$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator))
$itemExit = $menu.Items.Add('結束')
$tray.ContextMenuStrip = $menu

$script:mode = Read-Mode

function Apply-Mode {
    if ($script:mode -eq 'on') {
        $prev = [KeepAwake.Native]::SetThreadExecutionState($ES_CONTINUOUS -bor $ES_SYSTEM_REQUIRED -bor $ES_DISPLAY_REQUIRED)
        $tray.Icon = $iconOn
        $tray.Text = 'keep-awake：開啟（系統與螢幕保持喚醒）'
    } else {
        $prev = [KeepAwake.Native]::SetThreadExecutionState($ES_CONTINUOUS)
        $tray.Icon = $iconOff
        $tray.Text = 'keep-awake：關（正常睡眠）'
    }
    $itemOn.Checked  = ($script:mode -eq 'on')
    $itemOff.Checked = ($script:mode -eq 'off')
    if ($prev -eq 0) { $tray.Text = 'keep-awake：SetThreadExecutionState 失敗' }
}

$itemOn.add_Click({ $script:mode = 'on'; Save-Mode 'on'; Apply-Mode })
$itemOff.add_Click({ $script:mode = 'off'; Save-Mode 'off'; Apply-Mode })
$tray.add_MouseClick({
    param($s, $e)
    if ($e.Button -eq [System.Windows.Forms.MouseButtons]::Left) {
        $script:mode = if ($script:mode -eq 'on') { 'off' } else { 'on' }
        Save-Mode $script:mode
        Apply-Mode
    }
})
$itemExit.add_Click({ [System.Windows.Forms.Application]::Exit() })

# 每 60 秒重新宣告一次（保險；ES_CONTINUOUS 本身就會持續有效）
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 60000
$timer.add_Tick({ if ($script:mode -eq 'on') { Apply-Mode } })
$timer.Start()

Apply-Mode
$tray.Visible = $true
try {
    [System.Windows.Forms.Application]::Run()
} finally {
    $timer.Stop()
    [void][KeepAwake.Native]::SetThreadExecutionState($ES_CONTINUOUS)
    $tray.Visible = $false
    $tray.Dispose()
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
