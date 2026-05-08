param(
    [string]$ShortcutPath = "",
    [string]$LauncherPath = ""
)

function Resolve-DefaultShortcutPath {
    # 默认把快捷方式放到当前用户桌面。
    $desktop = [Environment]::GetFolderPath("Desktop")
    return (Join-Path $desktop "PulseOx Monitor.lnk")
}

function Resolve-DefaultLauncherPath {
    # 默认指向项目内的通用启动脚本。
    return (Join-Path $PSScriptRoot "launch_monitor.cmd")
}

function New-LauncherShortcut {
    # 创建用于启动监护程序的 Windows 快捷方式。
    param(
        [Parameter(Mandatory = $true)]
        [string]$ShortcutFile,
        [Parameter(Mandatory = $true)]
        [string]$TargetFile
    )

    if (-not (Test-Path -LiteralPath $TargetFile)) {
        throw "Launcher script not found: $TargetFile"
    }

    # 如果目标目录不存在，则先创建目录，避免快捷方式保存失败。
    $shortcutDirectory = Split-Path -Path $ShortcutFile -Parent
    if ($shortcutDirectory -and -not (Test-Path -LiteralPath $shortcutDirectory)) {
        New-Item -ItemType Directory -Path $shortcutDirectory -Force | Out-Null
    }

    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($ShortcutFile)
    $shortcut.TargetPath = $TargetFile
    $shortcut.WorkingDirectory = $PSScriptRoot
    $shortcut.Description = "Launch the PulseOx MQTT monitor"
    $shortcut.Save()
}

if ([string]::IsNullOrWhiteSpace($ShortcutPath)) {
    $ShortcutPath = Resolve-DefaultShortcutPath
}

if ([string]::IsNullOrWhiteSpace($LauncherPath)) {
    $LauncherPath = Resolve-DefaultLauncherPath
}

New-LauncherShortcut -ShortcutFile $ShortcutPath -TargetFile $LauncherPath
Write-Host "Shortcut created at: $ShortcutPath"
