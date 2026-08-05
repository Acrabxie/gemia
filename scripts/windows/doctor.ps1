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

Push-Location $RepoRoot
try {
& $Python -c "import sys; assert sys.version_info >= (3, 12); import gemia, server; print(sys.version.split()[0])"
if ($LASTEXITCODE -ne 0) { throw "Python or Lumeri import validation failed." }

foreach ($CommandName in @("ffmpeg", "ffprobe")) {
    $Command = Get-Command $CommandName -ErrorAction SilentlyContinue
    if ($null -eq $Command) { throw "$CommandName is not available on PATH." }
    $VersionOutput = & $Command.Source -version
    $ExitCode = $LASTEXITCODE
    $VersionOutput | Select-Object -First 1
    if ($ExitCode -ne 0) { throw "$CommandName could not start." }
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
Write-Host "Secure arbitrary-code sandbox: unavailable on native Windows; build/run_shell stay locked unless the computer owner explicitly disables Sandbox in Lumeri." -ForegroundColor Yellow

$Codex = Get-Command codex -ErrorAction SilentlyContinue
if ($null -eq $Codex) {
    Write-Host "Optional OpenAI subscription: Codex CLI not found. Install Node.js, run 'npm install -g @openai/codex', then 'codex login'." -ForegroundColor Yellow
}
else {
    $CodexStatus = & $Codex.Source login status 2>&1
    if ($LASTEXITCODE -eq 0 -and ($CodexStatus -join " ") -match "ChatGPT") {
        Write-Host "Optional OpenAI subscription: local Codex is signed in with ChatGPT." -ForegroundColor Green
    }
    else {
        Write-Host "Optional OpenAI subscription: Codex is installed but needs 'codex login' with ChatGPT." -ForegroundColor Yellow
    }
}
}
finally {
    Pop-Location
}
