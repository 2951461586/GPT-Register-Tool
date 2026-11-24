<#
.SYNOPSIS
    Build the pure-Rust Sentinel VM cdylib (`sentinel_vm.dll`) and copy it to
    `sms_tool/sentinel/runtime/sentinel_vm.dll`.
#>
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$crateDir = Join-Path $repoRoot "native\sentinel_vm"
$outDir = Join-Path $repoRoot "sms_tool\sentinel\runtime"

Push-Location $crateDir
try {
    & cargo test --release
    if ($LASTEXITCODE -ne 0) { throw "cargo test failed with exit code $LASTEXITCODE" }
    & cargo build --release
    if ($LASTEXITCODE -ne 0) { throw "cargo build failed with exit code $LASTEXITCODE" }
} finally {
    Pop-Location
}

$built = Join-Path $crateDir "target\release\sentinel_vm.dll"
if (-not (Test-Path $built)) {
    throw "Expected compiled library at $built"
}
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$dest = Join-Path $outDir "sentinel_vm.dll"
Copy-Item $built $dest -Force
$hash = (Get-FileHash -Path $dest -Algorithm SHA256).Hash.ToLowerInvariant()
Write-Host "Built $dest ($( (Get-Item $dest).Length ) bytes) sha256=$hash"
