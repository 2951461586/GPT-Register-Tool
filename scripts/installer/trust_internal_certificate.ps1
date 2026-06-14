param(
    [string]$CertificatePath = ""
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($CertificatePath)) {
    $CertificatePath = Join-Path $PSScriptRoot "GPT-Register-Tool-Internal-CodeSigning.cer"
}

$resolved = Resolve-Path -LiteralPath $CertificatePath -ErrorAction Stop
$cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($resolved.Path)

Write-Host "Importing certificate: $($cert.Subject)"
Write-Host "Thumbprint: $($cert.Thumbprint)"

Import-Certificate -FilePath $resolved.Path -CertStoreLocation Cert:\CurrentUser\Root | Out-Null
Import-Certificate -FilePath $resolved.Path -CertStoreLocation Cert:\CurrentUser\TrustedPublisher | Out-Null

Write-Host "Imported into CurrentUser Trusted Root Certification Authorities and Trusted Publishers."
