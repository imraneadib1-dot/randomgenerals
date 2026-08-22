# Push RandomGenerals to GitHub.
#
# This exists because Git Credential Manager needs a real window to show
# its sign-in prompt, and an automated shell has nowhere to show one -
# the push just hangs until it's killed. Running this opens a normal
# terminal where the prompt can actually appear.
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..

Write-Host ""
Write-Host "  Pushing RandomGenerals AI to GitHub" -ForegroundColor Cyan
Write-Host "  ===================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  A GitHub sign-in window will open in your browser." -ForegroundColor Yellow
Write-Host "  Sign in as imraneadib1-dot, then come back here." -ForegroundColor Yellow
Write-Host ""

git push -u origin main

Write-Host ""
if ($LASTEXITCODE -eq 0) {
    Write-Host "  PUSH SUCCEEDED" -ForegroundColor Green
    Write-Host "  https://github.com/imraneadib1-dot/randomgenerals" -ForegroundColor Green
    Write-Host ""
    Write-Host "  Tell Claude it is done and Render setup can start." -ForegroundColor Cyan
} else {
    Write-Host "  PUSH FAILED (exit $LASTEXITCODE)" -ForegroundColor Red
    Write-Host "  Copy the error above and show it to Claude." -ForegroundColor Red
}
Write-Host ""
Write-Host "  This window stays open. Close it when you are done."
