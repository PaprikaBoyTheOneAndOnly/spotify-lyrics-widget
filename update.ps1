# Copies the widget's source files from this folder into the installed copy
# (%LOCALAPPDATA%\spotify-lyrics-widget) and restarts the widget if it is running.
# Run it from this folder:   powershell -ExecutionPolicy Bypass -File .\update.ps1
$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$dest = Join-Path $env:LOCALAPPDATA "spotify-lyrics-widget"
$files = @("lyrics_core.py", "lyrics_widget_windows.py")

if (-not (Test-Path $dest)) {
    Write-Host "The widget isn't installed yet ($dest is missing). Run .\install.ps1 first." -ForegroundColor Red
    exit 1
}

$changed = $false
foreach ($f in $files) {
    $src = Join-Path $here $f
    $dst = Join-Path $dest $f
    if (-not (Test-Path $src)) { throw "Missing $src" }
    if (-not (Test-Path $dst) -or (Get-FileHash $src).Hash -ne (Get-FileHash $dst).Hash) {
        Copy-Item $src $dst -Force
        Write-Host "updated: $f"
        $changed = $true
    }
}
if (-not $changed) {
    Write-Host "The installed widget is already up to date."
    exit 0
}

# Changes only take effect after a restart.
$running = Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*lyrics_widget_windows.py*" -and $_.ProcessId -ne $PID }
if ($running) {
    $exe = ($running | Select-Object -First 1).ExecutablePath
    $running | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Start-Sleep -Seconds 1
    Start-Process -FilePath $exe -ArgumentList "`"$(Join-Path $dest 'lyrics_widget_windows.py')`"" -WorkingDirectory $dest
    Write-Host "Widget restarted."
} else {
    Write-Host "The widget isn't running; the update applies the next time it starts."
}
