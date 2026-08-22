<#
.SYNOPSIS
  Runs RandomGenerals AI as a supervised service: starts the Flask app
  and the public tunnel, and restarts either one if it dies.

.DESCRIPTION
  The app has been run by hand all along, which is why it kept vanishing
  between sessions - nothing was watching it. This script is the missing
  piece: a loop that checks both processes every few seconds and brings
  back whichever stopped.

  Run it directly to supervise in the foreground:
      powershell -ExecutionPolicy Bypass -File scripts\serve.ps1

  Or install it to start automatically at logon:
      powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1

.PARAMETER Port
  Port for the hardened public instance. Debug is always off here - the
  Werkzeug debugger is a remote code execution hole on a public port.

.PARAMETER NoTunnel
  Serve on localhost only, without exposing a public URL.
#>
[CmdletBinding()]
param(
    [int]$Port = 5001,
    [switch]$NoTunnel
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $repoRoot "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$appLog = Join-Path $logDir "app.log"
$tunnelLog = Join-Path $logDir "tunnel.log"
$urlFile = Join-Path $logDir "public-url.txt"

function Write-Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Write-Host $line
    Add-Content -Path (Join-Path $logDir "supervisor.log") -Value $line
}

function Start-App {
    Write-Log "starting Flask on port $Port"
    return Start-Process -FilePath "python" `
        -ArgumentList "app.py" `
        -WorkingDirectory $repoRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $appLog `
        -RedirectStandardError (Join-Path $logDir "app.err.log") `
        -PassThru
}

$tunnelConfig = Join-Path $repoRoot "cloudflared-config.yml"
$tunnelName = "randomgenerals"

function Start-Tunnel {
    # Prefer the named tunnel: it keeps ONE stable hostname
    # (randomgenerals.com) across restarts. The quick-tunnel fallback
    # below is only for a machine that hasn't been through
    # `cloudflared tunnel login` - it works, but hands out a new random
    # hostname every time, which silently breaks the Stripe webhook.
    if (Test-Path $tunnelConfig) {
        Write-Log "starting named tunnel '$tunnelName' -> localhost:$Port"
        return Start-Process -FilePath "cloudflared" `
            -ArgumentList "tunnel --config `"$tunnelConfig`" run $tunnelName" `
            -WindowStyle Hidden `
            -RedirectStandardOutput $tunnelLog `
            -RedirectStandardError (Join-Path $logDir "tunnel.err.log") `
            -PassThru
    }
    Write-Log "no tunnel config found - falling back to a quick tunnel"
    return Start-Process -FilePath "cloudflared" `
        -ArgumentList "tunnel --url http://localhost:$Port" `
        -WindowStyle Hidden `
        -RedirectStandardOutput $tunnelLog `
        -RedirectStandardError (Join-Path $logDir "tunnel.err.log") `
        -PassThru
}

function Get-TunnelUrl {
    # A named tunnel's hostname is fixed and comes from the config, not
    # from anything cloudflared prints at runtime.
    if (Test-Path $tunnelConfig) {
        $m = Select-String -Path $tunnelConfig -Pattern "hostname:\s*(\S+)" -ErrorAction SilentlyContinue |
             Select-Object -First 1
        if ($m) { return "https://" + $m.Matches[0].Groups[1].Value }
    }
    # Quick tunnel: the random URL is only ever printed to stderr, once.
    foreach ($f in @((Join-Path $logDir "tunnel.err.log"), $tunnelLog)) {
        if (Test-Path $f) {
            $m = Select-String -Path $f -Pattern "https://[a-z0-9-]+\.trycloudflare\.com" -ErrorAction SilentlyContinue |
                 Select-Object -Last 1
            if ($m) { return $m.Matches[0].Value }
        }
    }
    return $null
}

function Test-AppHealthy {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/" -UseBasicParsing -TimeoutSec 8
        return $r.StatusCode -eq 200
    } catch { return $false }
}

# Debug OFF and mock upgrades OFF: this instance is internet-facing.
$env:APP_DEBUG = "0"
$env:PORT = "$Port"
$env:ALLOW_MOCK_UPGRADE = "0"

Write-Log "=== supervisor starting (port $Port, tunnel: $(-not $NoTunnel)) ==="

# Clear out anything already holding the port, so a restart doesn't end
# up with two servers and a confusing half-working state.
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*app.py*" } |
    ForEach-Object {
        Write-Log "stopping stale python pid $($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
Start-Sleep -Seconds 2

$app = Start-App
$tunnel = $null
if (-not $NoTunnel) {
    Start-Sleep -Seconds 6   # let Flask bind before pointing a tunnel at it
    $tunnel = Start-Tunnel
    Start-Sleep -Seconds 10
    $url = Get-TunnelUrl
    if ($url) {
        # WriteAllText, not Set-Content -Encoding utf8: the latter emits a
        # UTF-8 BOM on Windows PowerShell, and anything reading this file
        # as plain UTF-8 then gets a stray ﻿ glued to the front of
        # the URL.
        [System.IO.File]::WriteAllText($urlFile, $url, (New-Object System.Text.UTF8Encoding $false))
        Write-Log "public URL: $url"
    } else {
        Write-Log "WARN: could not read tunnel URL from log yet"
    }
}

$failedChecks = 0
while ($true) {
    Start-Sleep -Seconds 15

    if ($app.HasExited) {
        Write-Log "app exited (code $($app.ExitCode)) - restarting"
        $app = Start-App
        Start-Sleep -Seconds 6
        $failedChecks = 0
        continue
    }

    # A process can be alive but wedged, so health is checked over HTTP
    # rather than by asking whether the pid still exists. Two strikes
    # before restarting, so one slow response doesn't cause a bounce.
    if (-not (Test-AppHealthy)) {
        $failedChecks++
        Write-Log "health check failed ($failedChecks/2)"
        if ($failedChecks -ge 2) {
            Write-Log "restarting unresponsive app"
            Stop-Process -Id $app.Id -Force -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 2
            $app = Start-App
            Start-Sleep -Seconds 6
            $failedChecks = 0
        }
        continue
    }
    $failedChecks = 0

    if (-not $NoTunnel -and $tunnel -and $tunnel.HasExited) {
        Write-Log "tunnel exited - restarting"
        $tunnel = Start-Tunnel
        Start-Sleep -Seconds 10
        $newUrl = Get-TunnelUrl
        if ($newUrl) {
            [System.IO.File]::WriteAllText($urlFile, $newUrl, (New-Object System.Text.UTF8Encoding $false))
            # A quick tunnel gets a NEW random hostname every restart, so
            # this is also the moment any Stripe webhook pointing at the
            # old one silently stops working.
            Write-Log "public URL CHANGED: $newUrl"
            Write-Log "  -> Stripe webhook must be re-registered for this URL"
        }
    }
}
