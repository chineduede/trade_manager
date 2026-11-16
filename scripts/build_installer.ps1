# Build the installer: PyInstaller exe first if missing, then Inno Setup.
#
#   powershell -ExecutionPolicy Bypass -File scripts\build_installer.ps1 [-Rebuild]
#
# -Rebuild forces a fresh PyInstaller build even when dist\mt5copier.exe exists.
# The version is read from pyproject.toml so the installer never drifts from the package.

param([switch]$Rebuild)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# The version is the single source of truth in pyproject.toml.
$pyproject = Get-Content "pyproject.toml" -Raw
if ($pyproject -notmatch '(?m)^version\s*=\s*"([^"]+)"') {
    throw "could not read version from pyproject.toml"
}
$version = $Matches[1]

if ($Rebuild -or -not (Test-Path "dist\mt5copier.exe")) {
    Write-Host "building dist\mt5copier.exe (PyInstaller)..."
    uv run pyinstaller copier.spec --noconfirm
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }
    Write-Host "REMINDER: read build\mt5copier\warn-mt5copier.txt before trusting the result."
}

# Find ISCC: PATH first, then the default install locations (per-user and per-machine).
$iscc = (Get-Command "iscc" -ErrorAction SilentlyContinue).Source
if (-not $iscc) {
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles(x86)\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )
    $iscc = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $iscc) {
    throw "Inno Setup not found. Install it with: winget install -e --id JRSoftware.InnoSetup"
}

& $iscc "/DAppVersion=$version" "installer.iss"
if ($LASTEXITCODE -ne 0) { throw "ISCC failed" }
Write-Host "installer: dist\mt5copier-setup-$version.exe"
