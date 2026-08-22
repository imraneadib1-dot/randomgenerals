# Compiles rnn_core.cpp into rnn_core.dll.
#
# Needs a MinGW-w64 g++ on PATH (installed via:
#   winget install --id BrechtSanders.WinLibs.POSIX.UCRT -e
# ). -march=native tunes for the machine that runs this script, not a
# portable target - rebuild on any other machine before using it there.
$ErrorActionPreference = "Stop"
Push-Location $PSScriptRoot
try {
    g++ -O3 -march=native -fopenmp -shared -static -o rnn_core.dll rnn_core.cpp
    if ($LASTEXITCODE -ne 0) { throw "g++ failed" }
    Write-Host "built rnn_core.dll"
} finally {
    Pop-Location
}
