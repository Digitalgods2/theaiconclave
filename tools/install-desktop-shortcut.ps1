# Create a desktop shortcut for launching AI Switchboard.
#
# Usage (from a normal PowerShell, no admin needed):
#   powershell -ExecutionPolicy Bypass -File .\tools\install-desktop-shortcut.ps1
#
# What this does:
#   - Finds <repo>\launch.pyw and <repo>\ai-switchboard.ico relative to this
#     script's location (one level up).
#   - Creates "AI Switchboard.lnk" on the current user's Desktop.
#   - The shortcut runs pythonw.exe with launch.pyw, hides the console, sets
#     the working directory to the repo (so DR0016 dev-mode detection fires).
#   - Sets the AI Conclave logo as the shortcut's icon.
#
# Re-running this script overwrites the existing shortcut — idempotent.
#
# To pin to the taskbar: right-click the desktop shortcut after this script
# finishes -> Pin to taskbar.

$ErrorActionPreference = "Stop"

$repoRoot   = Resolve-Path (Join-Path $PSScriptRoot "..")
$launchPath = Join-Path $repoRoot "launch.pyw"
$iconPath   = Join-Path $repoRoot "ai-switchboard.ico"

if (-not (Test-Path $launchPath)) { throw "launch.pyw not found at $launchPath" }
if (-not (Test-Path $iconPath))   { throw "ai-switchboard.ico not found at $iconPath" }

# Locate pythonw.exe. Prefer the one on PATH; fall back to common install dirs.
$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pythonw) {
    foreach ($candidate in @(
        "$env:LOCALAPPDATA\Programs\Python\Python313\pythonw.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\pythonw.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\pythonw.exe",
        "C:\Python313\pythonw.exe",
        "C:\Python312\pythonw.exe"
    )) {
        if (Test-Path $candidate) { $pythonw = $candidate; break }
    }
}
if (-not $pythonw) {
    throw "Could not find pythonw.exe on PATH. Install Python or add it to PATH, then re-run."
}

$desktop      = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktop "AI Switchboard.lnk"

$wshell = New-Object -ComObject WScript.Shell
$shortcut = $wshell.CreateShortcut($shortcutPath)
$shortcut.TargetPath       = $pythonw
$shortcut.Arguments        = "`"$launchPath`""
$shortcut.WorkingDirectory = "$repoRoot"
$shortcut.IconLocation     = "$iconPath,0"
$shortcut.Description      = "Launch the AI Switchboard service and open the dashboard."
$shortcut.WindowStyle      = 7      # 7 = Minimized; pythonw has no window anyway, this is a safety belt.
$shortcut.Save()

Write-Host "Created shortcut: $shortcutPath" -ForegroundColor Green
Write-Host "  target:     $pythonw `"$launchPath`""
Write-Host "  icon:       $iconPath"
Write-Host "  workingdir: $repoRoot"
Write-Host ""
Write-Host "Double-click the desktop icon to launch. To pin it to the taskbar:"
Write-Host "  right-click the desktop shortcut -> Pin to taskbar."
