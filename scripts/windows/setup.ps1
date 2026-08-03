[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$VenvRoot = Join-Path $RepoRoot ".venv"
$VenvPython = Join-Path $VenvRoot "Scripts\python.exe"

function Resolve-Python312 {
    $PyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($null -ne $PyLauncher) {
        & $PyLauncher.Source -3.12 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)"
        if ($LASTEXITCODE -eq 0) {
            return [PSCustomObject]@{ Executable = $PyLauncher.Source; Prefix = @("-3.12") }
        }
    }

    $Python = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $Python) {
        & $Python.Source -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)"
        if ($LASTEXITCODE -eq 0) {
            return [PSCustomObject]@{ Executable = $Python.Source; Prefix = @() }
        }
    }

    throw "Python 3.12 or newer was not found. Install it from python.org, then run this script again."
}

if ($null -eq (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    throw "FFmpeg was not found on PATH. Install an FFmpeg build for Windows, reopen PowerShell, and run this script again."
}
if ($null -eq (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
    throw "ffprobe was not found on PATH. Install the complete FFmpeg package, not an ffmpeg-only executable."
}

$PythonCommand = Resolve-Python312
Push-Location $RepoRoot
try {
    if (-not (Test-Path $VenvPython)) {
        & $PythonCommand.Executable @($PythonCommand.Prefix) -m venv $VenvRoot
        if ($LASTEXITCODE -ne 0) { throw "Creating .venv failed." }
    }

    & $VenvPython -m pip install --upgrade pip setuptools wheel
    if ($LASTEXITCODE -ne 0) { throw "Updating Python packaging tools failed." }

    & $VenvPython -m pip install -e .
    if ($LASTEXITCODE -ne 0) { throw "Installing Lumeri failed." }

    & (Join-Path $PSScriptRoot "doctor.ps1")
    if ($LASTEXITCODE -ne 0) { throw "Lumeri doctor checks failed." }
}
finally {
    Pop-Location
}

Write-Host ""
Write-Host "Lumeri is ready. Start it with:" -ForegroundColor Green
Write-Host "  .\scripts\windows\start.ps1"
