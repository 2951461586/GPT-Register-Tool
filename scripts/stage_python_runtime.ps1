<#
.SYNOPSIS
Stage the pinned CPython runtime used by the self-contained installer.

.DESCRIPTION
Downloads the interpreter recorded in `scripts/runtime_manifest.json`, verifies
its SHA-256 **before** extracting anything, unpacks it to `runtime/python`, and
installs `requirements.txt` into that interpreter. The result is the runtime the
WPF app prefers at startup (see `PythonPathResolver`), so an operator no longer
needs a system Python.

The archive digest is the security boundary: a mismatch removes the download and
fails the build. Never "fix" a mismatch by editing the digest without also
recording why in `scripts/runtime_manifest.json`'s review note.

.PARAMETER DryRun
Validate the manifest and print the resolved download/staging plan without
touching the network or the filesystem. CI uses this to keep the manifest and
script honest.

.PARAMETER Force
Re-stage even when a runtime already exists.

.PARAMETER SkipDependencies
Skip the `pip install -r requirements.txt` step (interpreter-only staging, used
by tests and by builds that install dependencies separately).
#>
param(
    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),
    [switch]$DryRun,
    [switch]$Force,
    [switch]$SkipDependencies
)

$ErrorActionPreference = "Stop"

$manifestPath = Join-Path $PSScriptRoot "runtime_manifest.json"
if (-not (Test-Path $manifestPath)) {
    throw "Runtime manifest is missing: $manifestPath"
}
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json

foreach ($field in @("schema", "release", "python_full_version", "asset", "url", "sha256", "runtime_subdir", "stamp_file")) {
    if ([string]::IsNullOrWhiteSpace([string]$manifest.$field)) {
        throw "runtime_manifest.json is missing '$field'"
    }
}
if ($manifest.schema -ne "python_runtime.v1") {
    throw "Unsupported runtime manifest schema: $($manifest.schema)"
}
if ($manifest.sha256 -notmatch '^[0-9a-f]{64}$') {
    throw "runtime_manifest.json sha256 must be 64 lowercase hex characters"
}
# The URL percent-encodes the build tag (``3.12.15+20261013``), so compare the
# decoded form: a raw substring match would reject the pinned asset forever.
$decodedUrl = [System.Uri]::UnescapeDataString($manifest.url)
if ($decodedUrl -notmatch [regex]::Escape($manifest.release)) {
    throw "runtime_manifest.json url does not reference release '$($manifest.release)'"
}
if ($decodedUrl -notmatch [regex]::Escape($manifest.asset)) {
    throw "runtime_manifest.json url does not reference asset '$($manifest.asset)'"
}

$runtimeRoot = Join-Path $RepoRoot $manifest.runtime_subdir
$pythonExe = Join-Path $runtimeRoot "python.exe"
$stampPath = Join-Path $runtimeRoot $manifest.stamp_file
$archivePath = Join-Path ([System.IO.Path]::GetTempPath()) $manifest.asset

$plan = [ordered]@{
    schema = $manifest.schema
    release = $manifest.release
    python = $manifest.python_full_version
    url = $manifest.url
    sha256 = $manifest.sha256
    runtime_root = $runtimeRoot
    stamp = $stampPath
}

if ($DryRun) {
    $plan | ConvertTo-Json -Depth 4
    exit 0
}

if ((Test-Path $pythonExe) -and -not $Force) {
    $stamp = if (Test-Path $stampPath) { Get-Content -LiteralPath $stampPath -Raw } else { "<no stamp>" }
    Write-Host "Runtime already staged: $pythonExe"
    Write-Host "Stamp: $stamp"
    exit 0
}

function Reset-RuntimeDirectory {
    param([string]$Path, [string]$AllowedRoot)
    $allowed = [System.IO.Path]::GetFullPath($AllowedRoot).TrimEnd('\') + '\'
    $target = [System.IO.Path]::GetFullPath($Path)
    if (-not $target.StartsWith($allowed, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to reset path outside ${allowed}: $target"
    }
    if (Test-Path $target) {
        Remove-Item -LiteralPath $target -Recurse -Force
    }
}

Write-Host "Downloading $($manifest.asset)..."
Invoke-WebRequest -Uri $manifest.url -OutFile $archivePath -UseBasicParsing
$archiveSize = (Get-Item -LiteralPath $archivePath).Length
Write-Host "Downloaded $([math]::Round($archiveSize / 1MB, 1)) MB"

$actual = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actual -ne $manifest.sha256) {
    Remove-Item -LiteralPath $archivePath -Force -ErrorAction SilentlyContinue
    throw "Runtime archive digest mismatch: expected $($manifest.sha256), got $actual. Refusing to extract."
}

$extractRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("gpt-register-runtime-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $extractRoot -Force | Out-Null
try {
    Write-Host "Extracting to a temporary directory..."
    # Resolve Windows' bsdtar explicitly. When this script runs from a Git Bash
    # environment, PATH finds /usr/bin/tar (GNU tar) first, which reads the
    # ``C:\...`` archive path as a remote host spec and fails with
    # "Cannot connect to C:". The system tar handles both gzip and the long
    # paths inside the runtime.
    $tarExe = Join-Path $env:SystemRoot "System32\tar.exe"
    if (-not (Test-Path $tarExe)) { $tarExe = "tar" }
    & $tarExe -xzf $archivePath -C $extractRoot
    if ($LASTEXITCODE -ne 0) {
        throw "tar failed with exit code $LASTEXITCODE"
    }
    $extractedPython = Join-Path $extractRoot "python"
    if (-not (Test-Path (Join-Path $extractedPython "python.exe"))) {
        throw "Archive did not contain the expected 'python/python.exe' layout"
    }

    Reset-RuntimeDirectory -Path $runtimeRoot -AllowedRoot $RepoRoot
    New-Item -ItemType Directory -Path (Split-Path -Parent $runtimeRoot) -Force | Out-Null
    Move-Item -LiteralPath $extractedPython -Destination $runtimeRoot
}
finally {
    Remove-Item -LiteralPath $extractRoot -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $archivePath -Force -ErrorAction SilentlyContinue
}

$version = (& $pythonExe -c "import sys; print('.'.join(map(str, sys.version_info[:3])))").Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($version)) {
    throw "Staged interpreter failed to run: $pythonExe"
}
Write-Host "Staged CPython $version"

$freezeHash = ""
if (-not $SkipDependencies) {
    $requirements = Join-Path $RepoRoot "requirements.txt"
    $constraints = Join-Path $RepoRoot "constraints.txt"
    if (-not (Test-Path $requirements) -or -not (Test-Path $constraints)) {
        throw "requirements.txt/constraints.txt are required to stage dependencies"
    }
    Write-Host "Installing runtime dependencies into the bundled interpreter..."
    & $pythonExe -m pip install --no-warn-script-location -r $requirements -c $constraints
    if ($LASTEXITCODE -ne 0) {
        throw "pip install failed with exit code $LASTEXITCODE"
    }
    $freeze = (& $pythonExe -m pip freeze) -join "`n"
    $freezePath = Join-Path $runtimeRoot "PIP-FREEZE.txt"
    [System.IO.File]::WriteAllText($freezePath, $freeze + "`n", (New-Object System.Text.UTF8Encoding($false)))
    $freezeHash = (Get-FileHash -LiteralPath $freezePath -Algorithm SHA256).Hash.ToLowerInvariant()
}

$stamp = [ordered]@{
    schema = $manifest.schema
    release = $manifest.release
    python_version = $version
    asset = $manifest.asset
    sha256 = $manifest.sha256
    requirements_sha256 = $freezeHash
    staged_at_utc = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
    source = $manifest.distribution
}
$stampJson = $stamp | ConvertTo-Json -Depth 4
[System.IO.File]::WriteAllText($stampPath, $stampJson + "`n", (New-Object System.Text.UTF8Encoding($false)))
Write-Host "Runtime stamp written: $stampPath"
$stamp | ConvertTo-Json -Depth 4
