# Cut a release: checks, version bump, tag, build, archived installer.
#
#   powershell -ExecutionPolicy Bypass -File scripts\release.ps1                # patch bump
#   powershell -ExecutionPolicy Bypass -File scripts\release.ps1 -Bump minor
#   powershell -ExecutionPolicy Bypass -File scripts\release.ps1 -Version 1.2.0
#   ... -SkipChecks     skips lint/type/tests (only when they just ran green)
#
# The pipeline, in order -- it stops at the first failure:
#   1. refuse a dirty tree or a branch other than master: the tag must name exactly
#      what was shipped, nothing uncommitted riding along;
#   2. ruff check, ruff format --check, mypy, pytest;
#   3. bump __version__ in src\mt5copier\__init__.py (the single version source) -- but first
#      the notes gate: release_notes.py's top entry must be the version being cut, so every
#      release ships with fresh What's new words or not at all;
#   4. commit the bump and tag it vX.Y.Z;
#   5. fresh PyInstaller exe + Inno Setup installer (via build_installer.ps1 -Rebuild);
#   6. copy the installer into releases\ -- the shipping shelf, one file per version,
#      never cleaned by rebuilds. What an end user gets is always a file from there;
#   7. push master and the tag when an origin remote exists -- the pushed tag makes
#      GitHub Actions rebuild the installer and publish it as a GitHub Release
#      (.github\workflows\release.yml), with the release_notes.py entry as the body.
#
# If step 5 fails: the tag already marks the right commit. Fix the build problem without
# touching version or tag, then run  scripts\build_installer.ps1 -Rebuild  and copy the
# installer to releases\ yourself.

param(
    [ValidateSet("patch", "minor", "major")]
    [string]$Bump = "patch",
    [string]$Version,
    [switch]$SkipChecks
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# -- 1. a clean tree on master ---------------------------------------------------------
$dirty = git status --porcelain
if ($dirty) {
    $list = $dirty -join "`n"
    throw "working tree is not clean -- commit or stash first:`n$list"
}
$branch = (git rev-parse --abbrev-ref HEAD).Trim()
if ($branch -ne "master") {
    throw "releases are cut from master, not '$branch'"
}

# -- 2. the gates ------------------------------------------------------------------------
if (-not $SkipChecks) {
    Write-Host "== ruff check =="
    uv run ruff check .
    if ($LASTEXITCODE -ne 0) { throw "ruff check failed" }
    Write-Host "== ruff format --check =="
    uv run ruff format --check .
    if ($LASTEXITCODE -ne 0) { throw "ruff format --check failed" }
    Write-Host "== mypy =="
    uv run mypy
    if ($LASTEXITCODE -ne 0) { throw "mypy failed" }
    Write-Host "== pytest =="
    uv run pytest -q
    if ($LASTEXITCODE -ne 0) { throw "tests failed" }
}

# -- 3. the bump -------------------------------------------------------------------------
$initPath = "src\mt5copier\__init__.py"
$initPy = Get-Content $initPath -Raw
if ($initPy -notmatch '(?m)^__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)"') {
    throw "could not read an x.y.z __version__ from $initPath"
}
$old = "$($Matches[1]).$($Matches[2]).$($Matches[3])"

if ($Version) {
    if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw "-Version must be x.y.z, got '$Version'" }
    $new = $Version
} else {
    $maj = [int]$Matches[1]; $min = [int]$Matches[2]; $pat = [int]$Matches[3]
    switch ($Bump) {
        "major" { $maj++; $min = 0; $pat = 0 }
        "minor" { $min++; $pat = 0 }
        "patch" { $pat++ }
    }
    $new = "$maj.$min.$pat"
}
if ($new -eq $old) { throw "new version $new equals the current one" }
if (git tag -l "v$new") { throw "tag v$new already exists" }

# The notes gate: What's new reads release_notes.py, and a release without words for itself
# is refused -- the words are written while the work is fresh, never reconstructed later.
$top = uv run python -c "from mt5copier.release_notes import NOTES; print(NOTES[0][0])"
if ($LASTEXITCODE -ne 0 -or -not $top) {
    throw "could not read the top entry of src\mt5copier\release_notes.py"
}
$top = @($top)[-1].Trim()
if ($top -ne $new) {
    throw "release_notes.py's top entry is $top, but v$new is being cut -- write this release's notes first"
}

Write-Host "version: $old -> $new"
$initPy = $initPy -replace '(?m)^__version__\s*=\s*"[^"]+"', "__version__ = `"$new`""
# WriteAllText: UTF-8 without BOM -- Windows PowerShell's Set-Content -Encoding utf8 would
# plant a BOM at the top of the Python file.
[System.IO.File]::WriteAllText((Join-Path $root $initPath), $initPy)

# -- 4. commit and tag -------------------------------------------------------------------
git add $initPath
git commit -m "Release v$new"
if ($LASTEXITCODE -ne 0) { throw "git commit failed" }
git tag -a "v$new" -m "mt5copier $new"
if ($LASTEXITCODE -ne 0) { throw "git tag failed" }

# -- 5. build ----------------------------------------------------------------------------
& "$PSScriptRoot\build_installer.ps1" -Rebuild

# -- 6. the shelf ------------------------------------------------------------------------
$installer = "dist\mt5copier-setup-$new.exe"
if (-not (Test-Path $installer)) { throw "expected $installer after the build" }
New-Item -ItemType Directory -Force "releases" | Out-Null
Copy-Item $installer "releases\" -Force

# -- 7. push -----------------------------------------------------------------------------
# Master takes everything else through pull requests; the release bump + tag ride the
# repository-admin bypass on purpose, so the one definition of a release stays this script.
$pushed = $false
if (@(git remote) -contains "origin") {
    git push origin master "v$new"
    if ($LASTEXITCODE -ne 0) { throw "git push failed -- push master and v$new by hand" }
    $pushed = $true
}

Write-Host ""
Write-Host "released v$new"
Write-Host "  ship this file:  releases\mt5copier-setup-$new.exe"
Write-Host "  tagged:          v$new  (commit $((git rev-parse --short HEAD).Trim()))"
if ($pushed) {
    Write-Host "  pushed:          master + v$new -- GitHub Actions is publishing the Release"
}
