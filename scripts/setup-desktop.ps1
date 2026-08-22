<#
.SYNOPSIS
  One-time setup for building the RandomGenerals AI desktop app.

.DESCRIPTION
  Checks for and installs the toolchain (Node.js), installs the Electron
  dependencies, and verifies the Python side is present. Run from the
  repo root:

      powershell -ExecutionPolicy Bypass -File scripts\setup-desktop.ps1

  This does NOT download the AI models - those are ~23GB and are fetched
  on first run of the app itself (Ollama pulls its own; the image model
  downloads from HuggingFace on first image request).
#>
[CmdletBinding()]
param(
    [switch]$SkipNodeInstall
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$desktopDir = Join-Path $repoRoot "desktop"

function Write-Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Write-Ok($msg) { Write-Host "  OK  $msg" -ForegroundColor Green }
function Write-Warn2($msg) { Write-Host "  !   $msg" -ForegroundColor Yellow }

Write-Step "Checking prerequisites"

# --- Python (runs the actual app) ---
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    throw "Python not found on PATH. Install Python 3.10+ from https://python.org and re-run."
}
$pyVersion = (& python --version 2>&1)
Write-Ok "$pyVersion  ($($python.Source))"

# --- Ollama (serves the language models) ---
$ollama = Get-Command ollama -ErrorAction SilentlyContinue
if ($ollama) {
    Write-Ok "Ollama found ($($ollama.Source))"
} else {
    Write-Warn2 "Ollama not found. The app needs it to answer anything."
    Write-Warn2 "Install from https://ollama.com/download, then: ollama pull llama3.2"
}

# --- VS Code CLI (optional, powers the 'Open in VS Code' feature) ---
if (Get-Command code -ErrorAction SilentlyContinue) {
    Write-Ok "VS Code 'code' command available"
} else {
    Write-Warn2 "VS Code 'code' command not on PATH - the editor integration will be hidden."
    Write-Warn2 "In VS Code: Ctrl+Shift+P -> 'Shell Command: Install code command in PATH'"
}

# --- Node.js (builds the desktop app) ---
$node = Get-Command node -ErrorAction SilentlyContinue
if (-not $node) {
    if ($SkipNodeInstall) {
        throw "Node.js not found and -SkipNodeInstall was set."
    }
    Write-Warn2 "Node.js not found - installing via winget (this takes a few minutes)"
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "winget unavailable. Install Node 20 LTS manually from https://nodejs.org and re-run."
    }
    winget install --id OpenJS.NodeJS.LTS -e --accept-package-agreements --accept-source-agreements
    # winget updates PATH for new shells only; refresh it for this one so
    # the npm step below doesn't fail on a fresh install.
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path", "User")
    $node = Get-Command node -ErrorAction SilentlyContinue
    if (-not $node) {
        throw "Node installed but not visible yet. Close this window, open a new one, and re-run."
    }
}
Write-Ok "Node $(& node --version)"

# --- Python dependencies ---
Write-Step "Installing Python dependencies"
$req = Join-Path $repoRoot "requirements.txt"
if (Test-Path $req) {
    & python -m pip install -r $req --quiet
    Write-Ok "requirements.txt installed"
} else {
    Write-Warn2 "No requirements.txt found - skipping"
}

# --- Electron dependencies ---
Write-Step "Installing Electron dependencies"
Push-Location $desktopDir
try {
    if (Test-Path "package-lock.json") { & npm ci } else { & npm install }
    if ($LASTEXITCODE -ne 0) { throw "npm install failed with exit code $LASTEXITCODE" }
    Write-Ok "node_modules ready"
} finally {
    Pop-Location
}

Write-Step "Done"
Write-Host @"
  Run in development:   cd desktop; npm run dev
  Build an installer:   cd desktop; npm run dist
                        -> desktop\dist\*.exe

  First launch will be slow: Ollama loads a multi-GB model into memory,
  and the image model downloads on first use. That is expected.
"@ -ForegroundColor Gray
