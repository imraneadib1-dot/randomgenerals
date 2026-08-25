<#
.SYNOPSIS
  Stops the RandomGenerals server and tunnel.

.DESCRIPTION
  The supervisor normally runs hidden (see start-hidden.vbs), so there is
  no window to close. This stops the supervisor first and then the two
  processes it watches - in that order, because stopping the app while
  the supervisor is alive just makes it start a new one.
#>
$me = $PID

Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.ProcessId -ne $me -and $_.CommandLine -like "*serve*" } |
  ForEach-Object {
    try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop
          Write-Host "stopped supervisor pid $($_.ProcessId)" } catch {}
  }

Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -like "*app.py*" } |
  ForEach-Object {
    try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop
          Write-Host "stopped flask pid $($_.ProcessId)" } catch {}
  }

Get-Process cloudflared -ErrorAction SilentlyContinue |
  ForEach-Object {
    try { Stop-Process -Id $_.Id -Force -ErrorAction Stop
          Write-Host "stopped tunnel pid $($_.Id)" } catch {}
  }

Write-Host "`nSite is now offline. Start it again with scripts\start-hidden.vbs"
