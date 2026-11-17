# The check gate: the same four checks a release runs, as one command.
#
#   powershell -ExecutionPolicy Bypass -File scripts\check.ps1          # everything
#   ... -NoTests     lint and types only (seconds, for a quick loop)
#
# It stops at the first failure. The pre-commit hook (scripts\githooks\pre-commit) runs
# this before every commit, and scripts\release.ps1 runs the same gates again at release
# time -- one definition of "green" everywhere.

param(
    [switch]$NoTests
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "== ruff check =="
uv run ruff check .
if ($LASTEXITCODE -ne 0) { exit 1 }

Write-Host "== ruff format --check =="
uv run ruff format --check .
if ($LASTEXITCODE -ne 0) { exit 1 }

Write-Host "== mypy =="
uv run mypy
if ($LASTEXITCODE -ne 0) { exit 1 }

if (-not $NoTests) {
    Write-Host "== pytest =="
    uv run pytest -q
    if ($LASTEXITCODE -ne 0) { exit 1 }
}

Write-Host ""
Write-Host "all checks green"
