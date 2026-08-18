# start.ps1 - pick a tunnel and start the kimi-remote session via uv.
# Usage:  powershell -ExecutionPolicy Bypass -File .\start.ps1 [-- extra args for kimi_remote.py]
#requires -Version 5.1
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

function Test-Cmd($name) { [bool](Get-Command $name -ErrorAction SilentlyContinue) }

if (-not (Test-Cmd uv)) {
    Write-Host "uv not found. Run .\setup.ps1 first." -ForegroundColor Red
    exit 1
}

# --- choose transport -----------------------------------------------------------
Write-Host "=== kimi-remote ===" -ForegroundColor Cyan
Write-Host "  [1] Tailscale  (private, tailnet-only; phone needs the Tailscale app)"
Write-Host "  [2] Cloudflare (public quick tunnel; nothing to install on the phone)"
$choice = Read-Host "Choose 1 or 2 [1]"
if (-not $choice) { $choice = '1' }

$pyArgs = @('kimi_remote.py')
if ($choice -eq '2') {
    if (-not (Test-Cmd cloudflared)) {
        Write-Host "cloudflared not found. Run .\setup.ps1 first." -ForegroundColor Red
        exit 1
    }
    $pyArgs += '--cloudflare'
} else {
    if (-not (Test-Cmd tailscale)) {
        Write-Host "tailscale not found. Run .\setup.ps1 first." -ForegroundColor Red
        exit 1
    }
    $status = (& tailscale status --json 2>$null | ConvertFrom-Json).BackendState
    if ($status -ne 'Running') {
        Write-Host "tailscale is not running - starting it (browser login may open)..." -ForegroundColor Yellow
        tailscale up
        if ($LASTEXITCODE -ne 0) {
            Write-Host "tailscale up failed. Fix the login, then re-run." -ForegroundColor Red
            exit 1
        }
    }
}

$pyArgs += $args   # pass through e.g. --port 58628

Write-Host "`nStarting (Ctrl-C stops everything)...`n" -ForegroundColor Cyan
uv run @pyArgs
