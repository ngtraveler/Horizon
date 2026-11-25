#Requires -Version 5.1
<#
.SYNOPSIS
    Start the Marine Radar wall dashboard on Windows.

.DESCRIPTION
    Runs collect.py once, then keeps a background loop that refreshes it every few
    minutes, and serves this folder over HTTP so the browser can load data.json
    (opening index.html via file:// would block the fetch). Ctrl+C stops both the
    server and the collector loop.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\start-dashboard.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\start-dashboard.ps1 -Port 8899 -Hours 48 -RefreshSeconds 300
#>
param(
    [int]$Port = 8899,
    [int]$Hours = 48,
    [int]$RefreshSeconds = 300
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

if (Get-Command py -ErrorAction SilentlyContinue) {
    $py = "py -3"
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $py = "python"
} else {
    throw "Python not found. Install it first: winget install -e --id Python.Python.3.12"
}

Write-Host "Python : $py"
Write-Host "Folder : $here"

# Collect once before serving, so the wall is never empty.
Invoke-Expression "$py collect.py --hours $Hours"
if ($LASTEXITCODE -ne 0) {
    Write-Warning "First collection failed; the page will show its error state."
}

$loop = {
    param($pyCmd, $hours, $seconds, $dir)
    Set-Location $dir
    while ($true) {
        Start-Sleep -Seconds $seconds
        try {
            Invoke-Expression "$pyCmd collect.py --hours $hours"
        } catch {
            Write-Warning "collect.py failed: $_"
        }
    }
}

$collector = Start-Job -ScriptBlock $loop -ArgumentList $py, $Hours, $RefreshSeconds, $here
Write-Host "Collector : every $RefreshSeconds s (background job)"
Write-Host "Dashboard : http://127.0.0.1:$Port/index.html"
Write-Host "Press Ctrl+C to stop."

try {
    Invoke-Expression "$py serve.py --port $Port --bind 127.0.0.1"
} finally {
    Stop-Job $collector -ErrorAction SilentlyContinue
    Remove-Job $collector -Force -ErrorAction SilentlyContinue
    Write-Host "Dashboard stopped."
}
