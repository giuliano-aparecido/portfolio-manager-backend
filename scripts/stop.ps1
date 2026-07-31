# Stops the local FastAPI dev server and Postgres (Docker) started by
# start.ps1. Looks up whatever's actually listening on port 8000 rather
# than tracking a saved PID, since uvicorn/Windows process trees make a
# saved parent PID unreliable to stop cleanly.

$ErrorActionPreference = 'Continue'
$repoRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $repoRoot 'docker-compose.yml'

$conn = Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue
if ($conn) {
    Write-Host "Stopping backend (PID $($conn.OwningProcess))..."
    Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue
} else {
    Write-Host "Nothing listening on port 8000."
}

Write-Host "Stopping Postgres (Docker)..."
docker compose -f $composeFile stop
