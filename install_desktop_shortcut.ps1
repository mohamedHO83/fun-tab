$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$desktop = [Environment]::GetFolderPath("Desktop")
$ico = Join-Path $root "assets\fun-tab.ico"
$exe = Join-Path $root "dist\FunTab\FunTab.exe"
$bat = Join-Path $root "run.bat"

if (-not (Test-Path $ico)) { throw "Missing icon at $ico" }

$shell = New-Object -ComObject WScript.Shell
$lnkPath = Join-Path $desktop "Fun Tab.lnk"
$shortcut = $shell.CreateShortcut($lnkPath)
$shortcut.IconLocation = "$ico,0"
$shortcut.Description = "Fun Tab - GTA-style radial Alt+Tab wheel"

if (Test-Path $exe) {
    $shortcut.TargetPath = $exe
    $shortcut.WorkingDirectory = Split-Path $exe
    $shortcut.WindowStyle = 1
} else {
    if (-not (Test-Path $bat)) { throw "Missing run.bat at $bat" }
    $shortcut.TargetPath = $bat
    $shortcut.WorkingDirectory = $root
    $shortcut.WindowStyle = 7  # minimized: the bat flashes less
}

$shortcut.Save()
Write-Host "Desktop shortcut created: $lnkPath"
