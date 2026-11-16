# Build the installer: PyInstaller exe first if missing, then Inno Setup.
#
#   powershell -ExecutionPolicy Bypass -File scripts\build_installer.ps1 [-Rebuild]
#
# -Rebuild forces a fresh PyInstaller build even when dist\mt5copier.exe exists.
# The version is read from src\mt5copier\__init__.py (the single source; pyproject.toml
# reads the same attribute) so the installer never drifts from the package.

param([switch]$Rebuild)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$initPy = Get-Content "src\mt5copier\__init__.py" -Raw
if ($initPy -notmatch '(?m)^__version__\s*=\s*"([^"]+)"') {
    throw "could not read __version__ from src\mt5copier\__init__.py"
}
$version = $Matches[1]

if ($Rebuild -or -not (Test-Path "dist\mt5copier.exe")) {
    Write-Host "building dist\mt5copier.exe (PyInstaller)..."
    uv run pyinstaller copier.spec --noconfirm
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }
    Write-Host "REMINDER: read build\copier\warn-copier.txt before trusting the result."
}

# Sandbox the freshly built exe: with this marker beside it, a smoke-test run from dist\
# writes to dist\data\ instead of the installed app's %APPDATA%\MT5Copier. The installer
# packages the exe alone, so the marker never reaches an end user's machine.
Set-Content -Path "dist\portable.txt" -Encoding ascii -Value "dev sandbox marker - a run from dist\ must not touch the installed app's data (see paths.base_data_dir)"

# The WebView2 Evergreen bootstrapper (~2MB) rides inside the installer so a machine
# without the runtime (pre-updated Windows 10, fresh images) gets it silently. Fetched at
# build time rather than committed; the fwlink is Microsoft's stable alias for it.
$bootstrapper = "build\MicrosoftEdgeWebView2Setup.exe"
if (-not (Test-Path $bootstrapper)) {
    Write-Host "downloading WebView2 bootstrapper..."
    New-Item -ItemType Directory -Force "build" | Out-Null
    Invoke-WebRequest -Uri "https://go.microsoft.com/fwlink/p/?LinkId=2124703" -OutFile $bootstrapper
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
