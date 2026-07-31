# Starts local Postgres (Docker) and the FastAPI dev server in the
# background. Safe to re-run - skips anything already running.
#
# Note: runs uvicorn WITHOUT --reload. This script is for quickly getting
# a working backend up (e.g. to test the frontend against), not for active
# backend development - if you're editing backend code and want hot reload,
# run `uvicorn app.main:app --reload` directly in its own terminal instead.

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $repoRoot 'docker-compose.yml'
$logFile = Join-Path $repoRoot '.dev-server.log'

Write-Host "Starting Postgres (Docker)..."
docker compose -f $composeFile up -d

Write-Host "Waiting for Postgres to become healthy..."
$healthy = $false
for ($i = 0; $i -lt 30; $i++) {
    $status = docker inspect --format='{{.State.Health.Status}}' portfolio-backend-postgres 2>$null
    if ($status -eq 'healthy') { $healthy = $true; break }
    Start-Sleep -Seconds 1
}
if (-not $healthy) {
    Write-Warning "Postgres did not report healthy in time - continuing anyway."
}

if (Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue) {
    Write-Host "Something is already listening on port 8000 - leaving it alone. Run stop.ps1 first to restart."
    exit 0
}

# Explicitly loads .env into this process's environment before spawning
# uvicorn, so a machine/user-level environment variable of the same name
# (e.g. a DATABASE_URL left over from an unrelated project) can't silently
# override it - pydantic-settings has no way to tell the difference and
# would otherwise just use whichever value the OS handed it.
$envFile = Join-Path $repoRoot '.env'
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*#' -or $_ -notmatch '=') { return }
        $key, $value = $_ -split '=', 2
        [System.Environment]::SetEnvironmentVariable($key.Trim(), $value.Trim(), 'Process')
    }
} else {
    Write-Warning "No .env found at $envFile - falling back to whatever's already in the environment. See DEVELOPMENT.md."
}

Write-Host "Starting FastAPI on http://localhost:8000 (background, logging to $logFile)..."
$venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    throw "No .venv found at $venvPython - see DEVELOPMENT.md for first-time setup."
}

Start-Process -FilePath $venvPython `
    -ArgumentList '-m', 'uvicorn', 'app.main:app', '--host', '0.0.0.0', '--port', '8000' `
    -WorkingDirectory $repoRoot `
    -RedirectStandardOutput $logFile `
    -RedirectStandardError "$logFile.err" `
    -WindowStyle Hidden

$up = $false
for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 1
    try {
        Invoke-RestMethod http://localhost:8000/health -TimeoutSec 2 | Out-Null
        $up = $true
        break
    } catch {}
}
if ($up) {
    Write-Host "Backend is up: http://localhost:8000"
} else {
    Write-Warning "Backend didn't respond within 15s - check $logFile / $logFile.err."
}
