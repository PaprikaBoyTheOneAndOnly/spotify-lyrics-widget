# Installs the widget on Windows:
#   1. finds Python 3 (with Tkinter, which is part of the normal python.org installer)
#   2. copies the widget to %LOCALAPPDATA%\spotify-lyrics-widget
#   3. adds a Start menu shortcut for the settings and starts the widget at login (once you have logged in to Spotify)
# No pip packages are needed. Safe to run again at any time.
#
# Run it from this folder:   powershell -ExecutionPolicy Bypass -File .\install.ps1
$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$dest = Join-Path $env:LOCALAPPDATA "spotify-lyrics-widget"
$configDir = Join-Path $env:APPDATA "spotify-lyrics-widget"
$files = @("lyrics_core.py", "lyrics_widget_windows.py")
$startup = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup"
$programs = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"

# 1. Python with Tkinter
function Find-Python {
    $candidates = @(@("py", "-3"), @("python"), @("python3"))
    foreach ($c in $candidates) {
        if (-not (Get-Command $c[0] -ErrorAction SilentlyContinue)) { continue }
        $extra = @($c | Select-Object -Skip 1)
        try {
            $exe = & $c[0] @extra -c "import sys, tkinter; print(sys.executable)" 2>$null
        } catch { continue }
        if ($LASTEXITCODE -eq 0 -and $exe -and (Test-Path $exe)) { return $exe.Trim() }
    }
    return $null
}

$python = Find-Python
if (-not $python) {
    Write-Host @"
Python 3 with Tkinter was not found, so the widget can't be installed.

Install it, then open a NEW PowerShell window and run this script again:
  winget install Python.Python.3.12
or download it from https://www.python.org/downloads/windows/
(keep "tcl/tk and IDLE" ticked in the installer; the Microsoft Store version also works)
"@ -ForegroundColor Red
    exit 1
}
$pythonw = Join-Path (Split-Path -Parent $python) "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $python }   # pythonw = no console window
Write-Host "Using Python: $python"

# 2. the widget itself
foreach ($f in $files) {
    if (-not (Test-Path (Join-Path $here $f))) { throw "Missing $f next to install.ps1" }
}
New-Item -ItemType Directory -Force -Path $dest | Out-Null
foreach ($f in $files) { Copy-Item (Join-Path $here $f) (Join-Path $dest $f) -Force }
Write-Host "Copied the widget to $dest"
$script = Join-Path $dest "lyrics_widget_windows.py"

function New-Shortcut($path, $arguments, $description) {
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($path)
    $link.TargetPath = $pythonw
    $link.Arguments = $arguments
    $link.WorkingDirectory = $dest
    $link.Description = $description
    $link.Save()
}

# Start menu entry for the settings (works even while nothing is playing)
New-Shortcut (Join-Path $programs "Lyrics Widget Settings.lnk") "`"$script`" --settings" "Spotify lyrics widget settings"

# 3. autostart (only once the Spotify login exists: an autostarted widget has no terminal to ask in)
$autostartLink = Join-Path $startup "Spotify Lyrics Widget.lnk"
if (Test-Path (Join-Path $configDir "token.json")) {
    New-Shortcut $autostartLink "`"$script`"" "Shows lyrics of the song playing on Spotify"
    Write-Host "Autostart enabled: the widget starts at your next login."
    Write-Host "Start it now with:"
    Write-Host "  & `"$pythonw`" `"$script`""
} else {
    Write-Host ""
    Write-Host "One step left: log in to Spotify once (it asks for your Client ID and opens your browser):"
    Write-Host "  & `"$python`" `"$script`""
    Write-Host "Then run .\install.ps1 again to enable autostart."
}
