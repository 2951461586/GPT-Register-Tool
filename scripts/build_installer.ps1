param(
    [string]$Version = "",
    [switch]$SkipPublish
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$dotnet = Join-Path $repoRoot ".dotnet\dotnet.exe"
if (-not (Test-Path $dotnet)) {
    $dotnet = Join-Path $env:ProgramFiles "dotnet\dotnet.exe"
}
if (-not (Test-Path $dotnet)) {
    $dotnet = "dotnet"
}

if ([string]::IsNullOrWhiteSpace($Version)) {
    $Version = "v$(Get-Date -Format 'yyyy.MM.dd')"
}

$publishDir = Join-Path $repoRoot "dist\net10"
$installerRoot = Join-Path $repoRoot "dist\installer"
$packageDir = Join-Path $installerRoot "package"
$releaseDir = Join-Path $repoRoot "dist\release"
$installerProject = Join-Path $repoRoot "scripts\installer\GPTRegisterToolSetup.csproj"

function Reset-Directory {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$AllowedRoot
    )

    $allowed = [System.IO.Path]::GetFullPath($AllowedRoot).TrimEnd('\') + '\'
    $target = [System.IO.Path]::GetFullPath($Path)
    if (-not $target.StartsWith($allowed, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to reset path outside ${allowed}: $target"
    }

    if (Test-Path $target) {
        Remove-Item -LiteralPath $target -Recurse -Force
    }
    New-Item -ItemType Directory -Path $target -Force | Out-Null
}

function Copy-FilePreservingPath {
    param(
        [Parameter(Mandatory = $true)][string]$RelativePath,
        [Parameter(Mandatory = $true)][string]$DestinationRoot
    )

    $source = Join-Path $repoRoot $RelativePath
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        return
    }

    $destination = Join-Path $DestinationRoot $RelativePath
    $destinationDir = Split-Path -Parent $destination
    if (-not (Test-Path $destinationDir)) {
        New-Item -ItemType Directory -Path $destinationDir -Force | Out-Null
    }
    Copy-Item -LiteralPath $source -Destination $destination -Force
}

function Remove-PackagePath {
    param([Parameter(Mandatory = $true)][string]$RelativePath)

    $target = [System.IO.Path]::GetFullPath((Join-Path $packageDir $RelativePath))
    $allowed = [System.IO.Path]::GetFullPath($packageDir).TrimEnd('\') + '\'
    if (-not $target.StartsWith($allowed, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove path outside package: $target"
    }
    if (Test-Path $target) {
        Remove-Item -LiteralPath $target -Recurse -Force
    }
}

if (-not $SkipPublish) {
    & (Join-Path $repoRoot "SmsWorkbench\build_dotnet.ps1")
    if ($LASTEXITCODE -ne 0) {
        throw "SmsWorkbench publish failed with exit code $LASTEXITCODE"
    }
}

$desktopExe = Join-Path $publishDir "SmsWorkbench.exe"
if (-not (Test-Path $desktopExe)) {
    throw "Missing published desktop executable: $desktopExe"
}

Reset-Directory -Path $installerRoot -AllowedRoot (Join-Path $repoRoot "dist")
New-Item -ItemType Directory -Path $releaseDir -Force | Out-Null

$trackedFiles = & git -C $repoRoot ls-files
if ($LASTEXITCODE -ne 0) {
    throw "git ls-files failed with exit code $LASTEXITCODE"
}

foreach ($relative in $trackedFiles) {
    $normalized = $relative -replace '/', '\'
    if ($normalized -match '^(.agents|.claude|tests|SmsWorkbench\\bin|SmsWorkbench\\obj|scripts\\installer)(\\|$)') {
        continue
    }
    if ($normalized -match '^dist(\\|$)') {
        continue
    }
    if ([System.IO.Path]::GetFileName($normalized).EndsWith('~', [System.StringComparison]::Ordinal)) {
        continue
    }
    Copy-FilePreservingPath -RelativePath $normalized -DestinationRoot $packageDir
}

$publishPackageDir = Join-Path $packageDir "dist\net10"
New-Item -ItemType Directory -Path (Split-Path -Parent $publishPackageDir) -Force | Out-Null
Copy-Item -LiteralPath $publishDir -Destination (Split-Path -Parent $publishPackageDir) -Recurse -Force
Remove-PackagePath -RelativePath "dist\net10\runtime"

$optionalBinaries = @("ppgateway.exe")
foreach ($binary in $optionalBinaries) {
    $binaryPath = Join-Path $repoRoot $binary
    if (Test-Path -LiteralPath $binaryPath -PathType Leaf) {
        Copy-Item -LiteralPath $binaryPath -Destination (Join-Path $packageDir $binary) -Force
    }
}

@"
GPT-Register-Tool Windows package

Start the desktop UI with:
  dist\net10\SmsWorkbench.exe

First-run setup:
  1. Install Python and run: python -m pip install -r requirements.txt
  2. Copy config.example.json to config.json if the installer did not create it.
  3. Edit config.json with local mailbox, proxy, SMS, and payment settings.

Local runtime data is written under runtime\ and sessions\.
"@ | Set-Content -Path (Join-Path $packageDir "INSTALL-README.txt") -Encoding UTF8

@"
@echo off
setlocal
cd /d "%~dp0"
start "" "%~dp0dist\net10\SmsWorkbench.exe"
"@ | Set-Content -Path (Join-Path $packageDir "Start-SmsWorkbench.cmd") -Encoding ASCII

$safeVersion = ($Version -replace '[^0-9A-Za-z_.-]', '-')
$zipPath = Join-Path $releaseDir "GPT-Register-Tool-win-x64-$safeVersion.zip"
$setupPath = Join-Path $releaseDir "GPT-Register-Tool-Setup-$safeVersion.exe"
if (Test-Path $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}
if (Test-Path $setupPath) {
    Remove-Item -LiteralPath $setupPath -Force
}
Compress-Archive -Path (Join-Path $packageDir '*') -DestinationPath $zipPath -CompressionLevel Optimal

Copy-Item -LiteralPath $zipPath -Destination (Join-Path $repoRoot "scripts\installer\payload.zip") -Force
try {
    $installerPublishDir = Join-Path $installerRoot "setup-publish"
    & $dotnet publish $installerProject `
        -c Release `
        -r win-x64 `
        --self-contained true `
        -p:PublishSingleFile=true `
        -p:EnableCompressionInSingleFile=true `
        -p:DebugType=none `
        -p:DebugSymbols=false `
        -o $installerPublishDir
    if ($LASTEXITCODE -ne 0) {
        throw "installer publish failed with exit code $LASTEXITCODE"
    }

    Copy-Item -LiteralPath (Join-Path $installerPublishDir "GPTRegisterToolSetup.exe") -Destination $setupPath -Force
}
finally {
    Remove-Item -LiteralPath (Join-Path $repoRoot "scripts\installer\payload.zip") -Force -ErrorAction SilentlyContinue
}

$manifestPath = Join-Path $releaseDir "GPT-Register-Tool-$safeVersion.sha256.txt"
@(
    "$(Get-FileHash -Algorithm SHA256 $setupPath | Select-Object -ExpandProperty Hash)  $(Split-Path -Leaf $setupPath)",
    "$(Get-FileHash -Algorithm SHA256 $zipPath | Select-Object -ExpandProperty Hash)  $(Split-Path -Leaf $zipPath)"
) | Set-Content -Path $manifestPath -Encoding ASCII

Write-Host "Built installer: $setupPath"
Write-Host "Built portable zip: $zipPath"
Write-Host "Wrote checksums: $manifestPath"
