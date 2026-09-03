$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$desktop = [Environment]::GetFolderPath("Desktop")
$ico = Join-Path $root "assets\fun-tab.ico"
$bat = Join-Path $root "run.bat"

if (-not (Test-Path $bat)) { throw "Missing run.bat at $bat" }
if (-not (Test-Path $ico)) { throw "Missing icon at $ico" }

$shell = New-Object -ComObject WScript.Shell
$lnkPath = Join-Path $desktop "Fun Tab.lnk"
$shortcut = $shell.CreateShortcut($lnkPath)
$shortcut.TargetPath = $bat
$shortcut.WorkingDirectory = $root
$shortcut.IconLocation = "$ico,0"
$shortcut.Description = "Fun Tab - GTA-style Alt+Tab wheel"
$shortcut.WindowStyle = 7  # minimized: the bat flashes less
$shortcut.Save()

Write-Host "Desktop shortcut created: $lnkPath"
