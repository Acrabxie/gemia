[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 7788,
    [switch]$NoBrowser
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Url = "http://127.0.0.1:$Port/"

if (-not (Test-Path $Python)) {
    throw "Lumeri's .venv is missing. Run .\scripts\windows\setup.ps1 first."
}

& (Join-Path $PSScriptRoot "doctor.ps1") -Port $Port
if ($LASTEXITCODE -ne 0) { throw "Lumeri doctor checks failed." }

if (-not $NoBrowser) {
    Start-Job -ScriptBlock {
        param($TargetUrl)
        Start-Sleep -Seconds 2
        Start-Process $TargetUrl
    } -ArgumentList $Url | Out-Null
}

$env:LUMERI_HOST = "127.0.0.1"
$env:LUMERI_PORT = [string]$Port

Write-Host "Starting Lumeri at $Url" -ForegroundColor Cyan
Write-Host "Press Ctrl+C to stop it."

Push-Location $RepoRoot
try {
    & $Python -m gemia serve --host 127.0.0.1 --port $Port
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
