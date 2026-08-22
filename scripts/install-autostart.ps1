<#
.SYNOPSIS
  Registers (or removes) a Scheduled Task so the server starts itself at
  logon and stays up.

.DESCRIPTION
  Install:
      powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1
  Remove:
      powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1 -Uninstall

  Deliberately a per-user logon task, not a machine-wide service:
    * It needs no admin rights, so it cannot silently gain privilege.
    * It runs as you, which is what Ollama's GPU access expects - a
      SYSTEM service does not share your user session's GPU context.

  What it does NOT do is keep the machine awake. If the laptop sleeps,
  the site is down. See the note at the end of the script output.
#>
[CmdletBinding()]
param(
    [switch]$Uninstall,
    [int]$Port = 5001
)

$ErrorActionPreference = "Stop"
$taskName = "RandomGeneralsAI"
$repoRoot = Split-Path -Parent $PSScriptRoot
$serveScript = Join-Path $repoRoot "scripts\serve.ps1"

if ($Uninstall) {
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Host "Removed scheduled task '$taskName'." -ForegroundColor Green
    } else {
        Write-Host "No scheduled task named '$taskName' found." -ForegroundColor Yellow
    }
    Write-Host "Note: this does not stop a currently running server." -ForegroundColor Gray
    return
}

if (-not (Test-Path $serveScript)) {
    throw "Can't find $serveScript"
}

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$serveScript`" -Port $Port" `
    -WorkingDirectory $repoRoot

$trigger = New-ScheduledTaskTrigger -AtLogOn

# RestartCount covers the case where the supervisor itself dies (it
# supervises the app and tunnel, but nothing supervises it).
# ExecutionTimeLimit 0 = run indefinitely; the default would kill a
# long-running server after 72 hours.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "Replaced existing task." -ForegroundColor Yellow
}

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Runs the RandomGenerals AI server and public tunnel." | Out-Null

Write-Host "Installed scheduled task '$taskName'." -ForegroundColor Green
Write-Host @"

  Starts automatically at logon. To control it now:
    Start:  Start-ScheduledTask -TaskName $taskName
    Stop:   Stop-ScheduledTask  -TaskName $taskName
    Remove: powershell -File scripts\install-autostart.ps1 -Uninstall

  Logs:        logs\supervisor.log
  Public URL:  logs\public-url.txt

  IMPORTANT - this does not stop the machine sleeping. A laptop that
  sleeps takes the site down with it. For an always-on service:
    powercfg /change standby-timeout-ac 0
  (mains power only; leave battery sleep alone or you will flatten it.)
"@ -ForegroundColor Gray
