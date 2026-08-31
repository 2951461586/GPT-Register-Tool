param(
    [string]$Version = "",
    [switch]$SkipPublish,
    [switch]$SelfSign,
    [string]$CertificateSubject = "CN=GPT-Register-Tool Internal",
    [string]$CertificateExportName = "GPT-Register-Tool-Internal-CodeSigning.cer"
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
    # Single source of truth for the version is the latest git tag (vYYYY.MM.DD[.N]).
    # Falls back to today's date only when no tag is reachable (e.g. shallow clone).
    $tag = & git -C $repoRoot describe --tags --match='v*' --abbrev=0 2>$null
    if ($LASTEXITCODE -eq 0 -and $tag) {
        $Version = $tag.TrimStart('v')
    } else {
        $Version = "$(Get-Date -Format 'yyyy.MM.dd')"
    }
}

# 手工传参时也要 trim。此前只有自动取 tag 的分支做了 TrimStart('v')，于是
# -Version v2026.09.01 会原样进 `-p:Version=`，dotnet 直接报"不是有效的版本字符串"，
# 而 -Version 2026.09.01 虽能构建却产出 Setup-2026.09.01.exe —— 与历史资产
# 的 Setup-v2026.08.31.exe 命名不一致。两条路都不对，统一在这里归一。
$Version = $Version.TrimStart('v')

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

function Get-InternalCodeSigningCertificate {
    param([Parameter(Mandatory = $true)][string]$Subject)

    $now = Get-Date
    $cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert |
        Where-Object { $_.Subject -eq $Subject -and $_.NotAfter -gt $now } |
        Sort-Object NotAfter -Descending |
        Select-Object -First 1

    if ($null -ne $cert) {
        return $cert
    }

    Write-Host "Creating self-signed code signing certificate: $Subject"
    return New-SelfSignedCertificate `
        -Type CodeSigningCert `
        -Subject $Subject `
        -CertStoreLocation "Cert:\CurrentUser\My" `
        -KeyAlgorithm RSA `
        -KeyLength 3072 `
        -HashAlgorithm SHA256 `
        -KeyExportPolicy Exportable `
        -NotAfter $now.AddYears(5)
}

function Sign-FileWithCertificate {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][System.Security.Cryptography.X509Certificates.X509Certificate2]$Certificate
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Cannot sign missing file: $Path"
    }

    Write-Host "Signing $Path"
    $signature = Set-AuthenticodeSignature -FilePath $Path -Certificate $Certificate -HashAlgorithm SHA256
    if ($null -eq $signature.SignerCertificate) {
        throw "Signing failed for ${Path}: no signer certificate was written. Status: $($signature.Status) $($signature.StatusMessage)"
    }
    if ($signature.SignerCertificate.Thumbprint -ne $Certificate.Thumbprint) {
        throw "Signing failed for ${Path}: signer thumbprint $($signature.SignerCertificate.Thumbprint) did not match $($Certificate.Thumbprint)"
    }
    if ($signature.Status -ne 'Valid') {
        Write-Host "Signed with untrusted self-signed chain until the .cer is imported: $($signature.Status) $($signature.StatusMessage)"
    }
}

function Export-CodeSigningCertificate {
    param(
        [Parameter(Mandatory = $true)][System.Security.Cryptography.X509Certificates.X509Certificate2]$Certificate,
        [Parameter(Mandatory = $true)][string]$Path
    )

    $parent = Split-Path -Parent $Path
    if (-not (Test-Path $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    Export-Certificate -Cert $Certificate -FilePath $Path -Force | Out-Null
    Write-Host "Exported public signing certificate: $Path"
}

function Assert-AuthenticodeSigned {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][System.Security.Cryptography.X509Certificates.X509Certificate2]$Certificate
    )

    $signature = Get-AuthenticodeSignature -FilePath $Path
    if ($null -eq $signature.SignerCertificate) {
        throw "Missing Authenticode signature for ${Path}: $($signature.Status) $($signature.StatusMessage)"
    }
    if ($signature.SignerCertificate.Thumbprint -ne $Certificate.Thumbprint) {
        throw "Unexpected signer for ${Path}: $($signature.SignerCertificate.Subject)"
    }
    Write-Host "Verified self-signed signature: $Path ($($signature.SignerCertificate.Subject), trust status: $($signature.Status))"
}

$signingCert = $null
if ($SelfSign) {
    $signingCert = Get-InternalCodeSigningCertificate -Subject $CertificateSubject
}

if (-not $SkipPublish) {
    & (Join-Path $repoRoot "SmsWorkbench\build_dotnet.ps1") -Version $Version
    if ($LASTEXITCODE -ne 0) {
        throw "SmsWorkbench publish failed with exit code $LASTEXITCODE"
    }
}

$desktopExe = Join-Path $publishDir "SmsWorkbench.exe"
if (-not (Test-Path $desktopExe)) {
    throw "Missing published desktop executable: $desktopExe"
}

if ($SelfSign) {
    Sign-FileWithCertificate -Path $desktopExe -Certificate $signingCert
    Assert-AuthenticodeSigned -Path $desktopExe -Certificate $signingCert
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

@"
GPT-Register-Tool Windows package

Start the desktop UI with:
  dist\net10\SmsWorkbench.exe

First-run setup:
  1. Install Python 3.10+ (Add to PATH), then run:
     python -m pip install -r requirements.txt
  2. config.json is created from config.example.json on install; edit it with
     local mailbox, proxy, SMS, and payment settings (the desktop Settings
     window can edit most of them).
  3. Verify the environment any time with the built-in self-check:
     python chatgpt_phone_reg.py --doctor          (human-readable)
     python chatgpt_phone_reg.py --doctor --json   (machine-readable)
     The desktop app runs the same probe automatically on first launch and
     points at missing dependencies; the Python interpreter can be configured
     in Settings when it is not on PATH.

Local runtime data is written under runtime\ and sessions\.
"@ | Set-Content -Path (Join-Path $packageDir "INSTALL-README.txt") -Encoding UTF8

@"
@echo off
setlocal
cd /d "%~dp0"
start "" "%~dp0dist\net10\SmsWorkbench.exe"
"@ | Set-Content -Path (Join-Path $packageDir "Start-SmsWorkbench.cmd") -Encoding ASCII

# --- Release payload gate ----------------------------------------------------
# The payload is collected from `git ls-files` but copied from the working tree,
# so a file that git ignores can still be on disk and get shipped. On 2026-08-31
# that is exactly how a deleted diagnostic script carrying real credential
# prefixes reached the public release assets. Do not bypass this gate.
$pythonExe = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonExe)) {
    $pythonExe = "python"
}
$gateScript = Join-Path $repoRoot "scripts\scan_release_payload.py"
if (-not (Test-Path $gateScript)) {
    throw "Release payload gate is missing: $gateScript"
}
Write-Host "Scanning release payload for ignored or credential-bearing files..."
& $pythonExe $gateScript $packageDir
if ($LASTEXITCODE -ne 0) {
    throw "Release payload scan failed. Refusing to build the installer. Remove the flagged files and re-run the payload staging step."
}

# $Version 归一为不带 v（供 -p:Version= 用），文件名则统一带 v，与历史资产一致。
$safeVersion = 'v' + ($Version -replace '[^0-9A-Za-z_.-]', '-')
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
    if ($SelfSign) {
        Sign-FileWithCertificate -Path $setupPath -Certificate $signingCert
        Assert-AuthenticodeSigned -Path $setupPath -Certificate $signingCert
    }
}
finally {
    Remove-Item -LiteralPath (Join-Path $repoRoot "scripts\installer\payload.zip") -Force -ErrorAction SilentlyContinue
}

$certificatePath = $null
$trustScriptPath = $null
if ($SelfSign) {
    $certificatePath = Join-Path $releaseDir $CertificateExportName
    Export-CodeSigningCertificate -Certificate $signingCert -Path $certificatePath
    $trustScriptPath = Join-Path $releaseDir "trust_internal_certificate.ps1"
    Copy-Item -LiteralPath (Join-Path $repoRoot "scripts\installer\trust_internal_certificate.ps1") -Destination $trustScriptPath -Force
}

$hashLines = @(
    "$(Get-FileHash -Algorithm SHA256 $setupPath | Select-Object -ExpandProperty Hash)  $(Split-Path -Leaf $setupPath)",
    "$(Get-FileHash -Algorithm SHA256 $zipPath | Select-Object -ExpandProperty Hash)  $(Split-Path -Leaf $zipPath)"
)
if ($SelfSign -and $certificatePath) {
    $hashLines += "$(Get-FileHash -Algorithm SHA256 $certificatePath | Select-Object -ExpandProperty Hash)  $(Split-Path -Leaf $certificatePath)"
}
if ($SelfSign -and $trustScriptPath) {
    $hashLines += "$(Get-FileHash -Algorithm SHA256 $trustScriptPath | Select-Object -ExpandProperty Hash)  $(Split-Path -Leaf $trustScriptPath)"
}
$manifestPath = Join-Path $releaseDir "GPT-Register-Tool-$safeVersion.sha256.txt"
$hashLines | Set-Content -Path $manifestPath -Encoding ASCII

Write-Host "Built installer: $setupPath"
Write-Host "Built portable zip: $zipPath"
if ($SelfSign -and $certificatePath) {
    Write-Host "Built signing certificate: $certificatePath"
}
if ($SelfSign -and $trustScriptPath) {
    Write-Host "Built trust helper: $trustScriptPath"
}
Write-Host "Wrote checksums: $manifestPath"
