$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$dotnet = Join-Path $repoRoot ".dotnet\dotnet.exe"
if (-not (Test-Path $dotnet)) {
    $dotnet = Join-Path $env:ProgramFiles "dotnet\dotnet.exe"
}
if (-not (Test-Path $dotnet)) {
    $dotnet = "dotnet"
}

$project = Join-Path $PSScriptRoot "SmsWorkbench.csproj"
# Canonical runnable desktop artifact. The project bin/Release tree is an
# intermediate build location and should not be used as a second distribution.
$publishDir = Join-Path $repoRoot "dist\net10"

& $dotnet publish $project `
    -c Release `
    -r win-x64 `
    --self-contained false `
    -p:PublishSingleFile=false `
    -o $publishDir

if ($LASTEXITCODE -ne 0) {
    throw "dotnet publish failed with exit code $LASTEXITCODE"
}

$intermediateReleaseDir = Join-Path $PSScriptRoot "bin\Release\net10.0-windows"
$resolvedProjectDir = [System.IO.Path]::GetFullPath($PSScriptRoot)
if (Test-Path $intermediateReleaseDir) {
    $resolvedIntermediate = [System.IO.Path]::GetFullPath($intermediateReleaseDir)
    if (-not $resolvedIntermediate.StartsWith($resolvedProjectDir, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clean unexpected path: $resolvedIntermediate"
    }
    Remove-Item -LiteralPath $resolvedIntermediate -Recurse -Force
    Write-Host "Cleaned intermediate $resolvedIntermediate"
}

Write-Host "Published $publishDir\SmsWorkbench.exe"
