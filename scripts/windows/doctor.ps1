[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 7788
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    throw "Lumeri's .venv is missing. Run .\scripts\windows\setup.ps1 first."
}

& $Python -c "import sys; assert sys.version_info >= (3, 12); import gemia, server; print(sys.version.split()[0])"
if ($LASTEXITCODE -ne 0) { throw "Python or Lumeri import validation failed." }

foreach ($CommandName in @("ffmpeg", "ffprobe")) {
    $Command = Get-Command $CommandName -ErrorAction SilentlyContinue
    if ($null -eq $Command) { throw "$CommandName is not available on PATH." }
    & $Command.Source -version | Select-Object -First 1
    if ($LASTEXITCODE -ne 0) { throw "$CommandName could not start." }
}

$Listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
try {
    $Listener.Start()
}
catch {
    throw "Port $Port is already in use. Stop the existing process or choose another port with -Port."
}
finally {
    $Listener.Stop()
}

Write-Host "Windows runtime checks passed; loopback port $Port is available." -ForegroundColor Green
