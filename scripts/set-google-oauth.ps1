# Put the Google sign-in credentials on the server.
#
#     powershell -ExecutionPolicy Bypass -File scripts\set-google-oauth.ps1
#
# Asks for the two values from Google Cloud Console, sends them to the
# server over SSH, restarts the app and checks the result.
#
# WHY A SCRIPT RATHER THAN TWO COMMANDS
#
# The two commands are easy to get wrong in ways that fail quietly: the
# client secret pasted with a trailing space, the id pasted into the
# secret's slot, or either typed on the laptop instead of the server -
# where set-env.sh does not exist and the error says so in a way that
# reads like the server is broken. This asks for each by name, checks
# the shape before sending, and tells you whether the app agrees
# afterwards.
#
# The secret is read with -AsSecureString so it is not echoed to the
# screen and does not land in PowerShell's command history.

$ErrorActionPreference = "Stop"

$Host_    = "51.170.135.119"
$User     = "ubuntu"
$KeyPath  = Join-Path $env:USERPROFILE ".ssh\randomgenerals"
$SetEnv   = "/opt/randomgenerals/set-env.sh"

if (-not (Test-Path -LiteralPath $KeyPath)) {
  Write-Host "No SSH key at $KeyPath" -ForegroundColor Red
  Write-Host "That key is how this machine reaches the server. Without it"
  Write-Host "there is nothing to fix here - say so and it can be reissued."
  exit 1
}

Write-Host ""
Write-Host "Google sign-in credentials" -ForegroundColor Cyan
Write-Host "Both come from console.cloud.google.com > Clients > your Web client."
Write-Host ""

$clientId = (Read-Host "ID client (ends .apps.googleusercontent.com)").Trim()
if ($clientId -notmatch "\.apps\.googleusercontent\.com$") {
  Write-Host ""
  Write-Host "That does not look like a client ID - it should end in" -ForegroundColor Red
  Write-Host ".apps.googleusercontent.com. Nothing was sent." -ForegroundColor Red
  exit 1
}

# -AsSecureString: not echoed, and not left in the console history.
$secure = Read-Host "Code secret du client (starts GOCSPX-)" -AsSecureString
$plain  = [System.Net.NetworkCredential]::new("", $secure).Password.Trim()

if (-not $plain.StartsWith("GOCSPX-")) {
  Write-Host ""
  Write-Host "That does not look like a client secret - it should start" -ForegroundColor Red
  Write-Host "with GOCSPX-. Nothing was sent." -ForegroundColor Red
  Write-Host "(Did the id and the secret get swapped?)" -ForegroundColor Yellow
  exit 1
}

Write-Host ""
Write-Host "Sending to the server..." -ForegroundColor Cyan

# Single-quoted on the remote side so nothing in either value is
# interpreted by the server's shell.
$remote = "sudo $SetEnv GOOGLE_CLIENT_ID '$clientId' && " +
          "sudo $SetEnv GOOGLE_CLIENT_SECRET '$plain'"

& ssh -i $KeyPath -o StrictHostKeyChecking=no "$User@$Host_" $remote
if ($LASTEXITCODE -ne 0) {
  Write-Host "Setting the values failed - see the output above." -ForegroundColor Red
  exit 1
}

Write-Host ""
Write-Host "Checking the app agrees..." -ForegroundColor Cyan

# Ask the app itself rather than trusting that the write worked: this
# is the same function the sign-in route checks before offering Google.
$verify = "cd /opt/randomgenerals && ./.venv/bin/python -c " +
          "`"import app; print('CONFIGURED' if app.google_oauth_configured() " +
          "else 'NOT CONFIGURED')`""
& ssh -i $KeyPath -o StrictHostKeyChecking=no "$User@$Host_" $verify

Write-Host ""
Write-Host "If that said CONFIGURED, open https://randomgenerals.com/app" -ForegroundColor Green
Write-Host "and the Google button should work." -ForegroundColor Green
