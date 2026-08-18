# setup.ps1 - one-time environment setup for kimi-remote.
# Installs uv (Python runner) and your chosen tunnel CLI(s) via winget.
# Usage:  powershell -ExecutionPolicy Bypass -File .\setup.ps1
#requires -Version 5.1
$ErrorActionPreference = 'Stop'

function Test-Cmd($name) { [bool](Get-Command $name -ErrorAction SilentlyContinue) }

function Install-Winget($id, $name) {
    Write-Host "`n>>> winget install $id" -ForegroundColor Cyan
    winget install --id $id -e --accept-source-agreements --accept-package-agreements
    if ($LASTEXITCODE -ne 0) { Write-Host "!! $name install failed (exit $LASTEXITCODE)" -ForegroundColor Red }
    else {
        # refresh PATH so freshly installed tools are visible in this session
        $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                    [Environment]::GetEnvironmentVariable('Path', 'User')
        Write-Host "OK: $name installed" -ForegroundColor Green
    }
}

Write-Host "=== kimi-remote setup ===" -ForegroundColor Cyan

# --- prerequisite: winget -----------------------------------------------------
if (-not (Test-Cmd winget)) {
    Write-Host "winget not found. Install 'App Installer' from the Microsoft Store first." -ForegroundColor Red
    exit 1
}

# --- prerequisite: kimi code CLI ----------------------------------------------
if (Test-Cmd kimi) {
    Write-Host "OK: kimi CLI found" -ForegroundColor Green
} else {
    Write-Host "!! kimi CLI not found. Install kimi code first:" -ForegroundColor Red
    Write-Host "   https://www.kimi.com/code/docs/en/"
}

# --- choose tunnel CLIs -------------------------------------------------------
Write-Host "`nWhich tunnel do you want to use from your phone?"
Write-Host "  [1] Tailscale  (recommended: private, tailnet-only HTTPS; needs Tailscale app on phone)"
Write-Host "  [2] Cloudflare (quick tunnel: no account/app needed, but the URL is public)"
Write-Host "  [3] Both"
$choice = Read-Host "Choose 1, 2 or 3 [1]"
if (-not $choice) { $choice = '1' }
$wantTailscale  = $choice -in '1', '3'
$wantCloudflare = $choice -in '2', '3'

# --- uv -----------------------------------------------------------------------
if (Test-Cmd uv) {
    Write-Host "`nOK: uv found ($(uv --version))" -ForegroundColor Green
} else {
    Install-Winget 'astral-sh.uv' 'uv'
}

# --- tunnel CLIs ---------------------------------------------------------------
if ($wantTailscale) {
    if (Test-Cmd tailscale) {
        Write-Host "`nOK: tailscale found" -ForegroundColor Green
    } else {
        Install-Winget 'Tailscale.Tailscale' 'tailscale'
    }
    $status = (& tailscale status --json 2>$null | ConvertFrom-Json).BackendState
    if ($status -eq 'Running') {
        Write-Host "OK: tailscale is running and logged in" -ForegroundColor Green
    } else {
        Write-Host "`nTailscale needs a one-time login. Starting 'tailscale up' -" -ForegroundColor Yellow
        Write-Host "a browser window will open; log in, then come back here."
        Read-Host "Press Enter to run 'tailscale up'"
        tailscale up
    }
}

if ($wantCloudflare) {
    if (Test-Cmd cloudflared) {
        Write-Host "`nOK: cloudflared found" -ForegroundColor Green
    } else {
        Install-Winget 'Cloudflare.cloudflared' 'cloudflared'
    }
}

# --- done ----------------------------------------------------------------------
Write-Host "`n=== setup complete ===" -ForegroundColor Cyan
Write-Host "Start the remote session any time with:"
Write-Host "  powershell -ExecutionPolicy Bypass -File .\start.ps1"
