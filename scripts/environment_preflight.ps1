$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$globalJson = Get-Content (Join-Path $repoRoot "global.json") -Raw | ConvertFrom-Json
$requiredSdk = [string]$globalJson.sdk.version
$candidates = @(
    (Join-Path $repoRoot ".dotnet\dotnet.exe"),
    (Join-Path $env:ProgramFiles "dotnet\dotnet.exe"),
    "dotnet"
)

$dotnet = $null
foreach ($candidate in $candidates) {
    try {
        $command = Get-Command $candidate -ErrorAction Stop
        $hostPath = if ($command.Path) { $command.Path } else { $command.Source }
        $versionOutput = @(& $hostPath --version 2>&1)
        $hostExitCode = $LASTEXITCODE
        $version = ($versionOutput | Select-Object -First 1).ToString().Trim()
        if ($hostExitCode -eq 0 -and $version) {
            $dotnet = $hostPath
            break
        }
    } catch {
        continue
    }
}
if (-not $dotnet) {
    throw "No executable .NET host found; required SDK is $requiredSdk"
}

$requiredParts = $requiredSdk -split '\.'
$actualParts = $version -split '\.'
$compatibleFeatureBand = $requiredParts.Length -ge 3 -and $actualParts.Length -ge 3 -and
    $requiredParts[0] -eq $actualParts[0] -and
    $requiredParts[1] -eq $actualParts[1] -and
    [int]$actualParts[2] -ge [int]$requiredParts[2]
if (-not $compatibleFeatureBand) {
    throw "Required .NET SDK feature band $requiredSdk, found $version at $dotnet"
}

$installed = @(& $dotnet --list-sdks 2>&1)
if ($LASTEXITCODE -ne 0 -or -not ($installed | Where-Object { $_ -match "^$([regex]::Escape($version))\s" })) {
    throw ".NET host at $dotnet runs but SDK $version is not listed as installed"
}

# Collect first, select second: piping a native command into ``Select-Object
# -First 1`` raises StopUpstreamCommandsException under Windows PowerShell 5.1,
# which terminates the child and leaves ``$LASTEXITCODE`` at -1 -- so the gate
# rejected a perfectly good interpreter on 2026-11-19 while the same line passed
# in CI. Array capture does not truncate the pipeline.
$pythonProbe = @(& python --version 2>&1)
if ($LASTEXITCODE -ne 0 -or $pythonProbe.Count -lt 1) {
    throw "Python host is not executable"
}
$pythonVersion = ([string]$pythonProbe[0]).Trim()

$curlProbe = @(& python -c "import curl_cffi; from curl_cffi.requests.impersonate import BrowserType; print(curl_cffi.__version__); print(','.join(x.value for x in BrowserType))" 2>&1)
if ($LASTEXITCODE -ne 0 -or $curlProbe.Count -lt 2) {
    throw "curl_cffi profile probe failed; install requirements.txt in the isolated environment"
}
$curlVersion = [string]$curlProbe[0]
$profiles = [string]$curlProbe[1] -split ','

# The accepted minor range and the required profile are NOT repeated here.
# They live in ``sms_tool/auth_headers.py`` (``CURL_CFFI_MIN/MAX_MINOR`` and the
# newest entry of ``AUTH_FINGERPRINT_PROFILES``) because a local copy is what
# let this gate reject a healthy ``curl_cffi 0.17.0`` install on 2026-11-19 --
# the dependency bump and the two Python gates moved, the PowerShell copy did
# not. Ask the owner instead of restating it.
$authorityProbe = @'
from sms_tool.auth_headers import AUTH_FINGERPRINT_PROFILES, CURL_CFFI_MAX_MINOR, CURL_CFFI_MIN_MINOR
chrome = [n for n in AUTH_FINGERPRINT_PROFILES if n.startswith("chrome") and n[len("chrome"):].isdigit()]
print(CURL_CFFI_MIN_MINOR, CURL_CFFI_MAX_MINOR, max(chrome, key=lambda n: int(n[len("chrome"):])))
'@
Push-Location $repoRoot
try {
    $authority = @($authorityProbe | & python - 2>&1)
    $authorityExit = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($authorityExit -ne 0 -or $authority.Count -lt 1) {
    throw "curl_cffi authority probe failed; sms_tool/auth_headers.py is unreadable: $($authority -join ' ')"
}
$authorityParts = ([string]$authority[0]).Trim() -split '\s+'
if ($authorityParts.Length -lt 3) {
    throw "curl_cffi authority probe returned '$($authority[0])'; expected '<minMinor> <maxMinor> <profile>'"
}
$minMinor = [int]$authorityParts[0]
$maxMinor = [int]$authorityParts[1]
$requiredProfile = [string]$authorityParts[2]
if ($curlVersion -notmatch '^0\.(\d+)(\.|$)') {
    throw "curl_cffi $curlVersion is unsupported; required 0.$minMinor.x-0.$maxMinor.x"
}
$curlMinor = [int]$Matches[1]
if ($curlMinor -lt $minMinor -or $curlMinor -gt $maxMinor) {
    throw "curl_cffi $curlVersion is outside the supported range 0.$minMinor.x-0.$maxMinor.x (auth_headers.CURL_CFFI_MIN/MAX_MINOR)"
}
if ($profiles -notcontains $requiredProfile) {
    throw "curl_cffi $curlVersion lacks required browser profile $requiredProfile (newest AUTH_FINGERPRINT_PROFILES entry)"
}

# `python --version` already prints the "Python " prefix; do not say it twice.
$pythonLabel = $pythonVersion -replace '^Python\s+', ''
Write-Host "Environment ready: Python $pythonLabel; curl_cffi $curlVersion ($requiredProfile); .NET SDK $version ($dotnet)"
