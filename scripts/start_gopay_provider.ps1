param(
    [string]$ConfigPath = "config.json"
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$ConfigFullPath = Resolve-Path -LiteralPath (Join-Path $Root $ConfigPath)
$Config = Get-Content -LiteralPath $ConfigFullPath -Raw -Encoding UTF8 | ConvertFrom-Json
$GoPay = $Config.gopay
if ($null -eq $GoPay) {
    throw "config.json missing gopay section"
}

function Resolve-ProjectPath([string]$Value) {
    if ([string]::IsNullOrWhiteSpace($Value)) { return "" }
    $expanded = [Environment]::ExpandEnvironmentVariables($Value)
    if ([System.IO.Path]::IsPathRooted($expanded)) { return $expanded }
    return (Join-Path $Root $expanded)
}

function Ensure-Directory([string]$Path) {
    New-Item -ItemType Directory -Force -Path $Path | Out-Null
}

function First-Value($Values, [string]$Fallback = "") {
    foreach ($value in $Values) {
        if ($null -ne $value -and -not [string]::IsNullOrWhiteSpace([string]$value)) {
            return [string]$value
        }
    }
    return $Fallback
}

function Quote-Arg([string]$Value) {
    if ($null -eq $Value) { return '""' }
    return '"' + $Value.Replace('"', '\"') + '"'
}

function Write-JsonNoBom($Object, [string]$Path) {
    $json = $Object | ConvertTo-Json -Depth 20
    $encoding = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($Path, $json, $encoding)
}

$RuntimeDir = Join-Path $Root "runtime\gopay_provider"
Ensure-Directory $RuntimeDir

$PaymentAddr = First-Value @($GoPay.payment_service_addr) "127.0.0.1:50051"
$PaymentHost, $PaymentPortText = $PaymentAddr.Split(":", 2)
$PaymentPort = [int]$PaymentPortText
$SidecarAddr = First-Value @($GoPay.adb_sidecar_addr) "127.0.0.1:9999"
$SidecarHost, $SidecarPortText = $SidecarAddr.Split(":", 2)
$SidecarPort = [int]$SidecarPortText
$WaRebind = $GoPay.wa_rebind
$GoPayAppAddr = ""
$GoPayAppHost = ""
$GoPayAppPort = 0
if ($null -ne $WaRebind) {
    $GoPayAppAddr = First-Value @($WaRebind.gopay_app_service_addr, $GoPay.gopay_app_service_addr) ""
    if (![string]::IsNullOrWhiteSpace($GoPayAppAddr) -and $GoPayAppAddr.Contains(":")) {
        $GoPayAppHost, $GoPayAppPortText = $GoPayAppAddr.Split(":", 2)
        $GoPayAppPort = [int]$GoPayAppPortText
    }
}

$EmulatorExe = Resolve-ProjectPath (First-Value @($GoPay.emulator_exe) "D:\Program Files\Netease\MuMuPlayer\nx_main\MuMuNxMain.exe")
$AdbPath = Resolve-ProjectPath (First-Value @($GoPay.adb_path) "D:\Program Files\Netease\MuMuPlayer\nx_main\adb.exe")
$Serial = First-Value @($GoPay.adb_serial) "emulator-5554"

if (!(Test-Path -LiteralPath $AdbPath)) {
    throw "ADB not found: $AdbPath"
}
if ((Test-Path -LiteralPath $EmulatorExe) -and -not (Get-Process | Where-Object { $_.Path -eq $EmulatorExe })) {
    Start-Process -FilePath $EmulatorExe -WindowStyle Hidden
    Start-Sleep -Seconds 8
}

$TemplatePath = Resolve-ProjectPath (First-Value @($GoPay.provider_config_path) "services\gopay-flow\config.gopay.base.json")
if (!(Test-Path -LiteralPath $TemplatePath)) {
    throw "GoPay provider config template not found: $TemplatePath"
}
$ProviderConfig = Get-Content -LiteralPath $TemplatePath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($null -eq $ProviderConfig.gopay) {
    $ProviderConfig | Add-Member -NotePropertyName gopay -NotePropertyValue ([pscustomobject]@{})
}
$ProviderConfig.gopay.country_code = First-Value @($GoPay.country_code) "62"
$ProviderConfig.gopay.phone_number = First-Value @($GoPay.phone, $GoPay.phone_number) ""
$ProviderConfig.gopay.pin = First-Value @($GoPay.pin) ""
if ($null -eq $ProviderConfig.gopay.otp) {
    $ProviderConfig.gopay | Add-Member -NotePropertyName otp -NotePropertyValue ([pscustomobject]@{})
}
if ($null -eq $ProviderConfig.gopay.unlink) {
    $ProviderConfig.gopay | Add-Member -NotePropertyName unlink -NotePropertyValue ([pscustomobject]@{})
}
$ProviderConfig.gopay.otp.adb_url = "http://$SidecarAddr"
$ProviderConfig.gopay.unlink.adb_url = "http://$SidecarAddr"
$ProviderConfig.gopay.unlink.enabled = $true
if ($null -eq $ProviderConfig.adb) {
    $ProviderConfig | Add-Member -NotePropertyName adb -NotePropertyValue ([pscustomobject]@{})
}
$ProviderConfig.adb.adb_path = $AdbPath
$ProviderConfig.adb.serial = $Serial
$ProviderConfig.adb.package = "com.gojek.gopay"
$ProviderConfig.adb.post_unlink_back_steps = 1
$proxy = First-Value @($GoPay.proxy, $Config.proxy.default) ""
if (![string]::IsNullOrWhiteSpace($proxy)) {
    $ProviderConfig.proxy = $proxy
}
$GeneratedConfig = Join-Path $RuntimeDir "config.gopay.generated.json"
Write-JsonNoBom $ProviderConfig $GeneratedConfig

$SidecarListening = Get-NetTCPConnection -LocalAddress $SidecarHost -LocalPort $SidecarPort -ErrorAction SilentlyContinue
if (-not $SidecarListening) {
    $SidecarScript = Join-Path $Root "services\gopay-adb\gopay_adb_server.py"
    $SidecarArgs = @(
        (Quote-Arg $SidecarScript),
        "--listen", $SidecarAddr,
        "--config", (Quote-Arg $GeneratedConfig),
        "--adb-path", (Quote-Arg $AdbPath),
        "--serial", $Serial
    )
    Start-Process -FilePath python.exe -WindowStyle Hidden -WorkingDirectory $Root -ArgumentList @(
        $SidecarArgs
    ) -RedirectStandardOutput (Join-Path $RuntimeDir "adb_sidecar.log") -RedirectStandardError (Join-Path $RuntimeDir "adb_sidecar.err.log")
}

$PaymentListening = Get-NetTCPConnection -LocalAddress $PaymentHost -LocalPort $PaymentPort -ErrorAction SilentlyContinue
if (-not $PaymentListening) {
    $PaymentDir = Join-Path $Root "services\gopay-flow"
    $PaymentArgs = @(
        "payment_server.py",
        "--config", (Quote-Arg $GeneratedConfig),
        "--listen", $PaymentAddr,
        "--flow-ttl", "240"
    )
    Start-Process -FilePath python.exe -WindowStyle Hidden -WorkingDirectory $PaymentDir -ArgumentList @(
        $PaymentArgs
    ) -RedirectStandardOutput (Join-Path $RuntimeDir "payment_service.log") -RedirectStandardError (Join-Path $RuntimeDir "payment_service.err.log")
}

Start-Sleep -Seconds 2
$PaymentListening = Get-NetTCPConnection -LocalAddress $PaymentHost -LocalPort $PaymentPort -ErrorAction SilentlyContinue
$SidecarListening = Get-NetTCPConnection -LocalAddress $SidecarHost -LocalPort $SidecarPort -ErrorAction SilentlyContinue
$GoPayAppListening = $false
if ($GoPayAppPort -gt 0) {
    $GoPayAppListening = [bool](Get-NetTCPConnection -LocalAddress $GoPayAppHost -LocalPort $GoPayAppPort -ErrorAction SilentlyContinue)
}
[pscustomobject]@{
    payment_service_addr = $PaymentAddr
    payment_service_listening = [bool]$PaymentListening
    adb_sidecar_addr = $SidecarAddr
    adb_sidecar_listening = [bool]$SidecarListening
    gopay_app_service_addr = $GoPayAppAddr
    gopay_app_service_listening = [bool]$GoPayAppListening
    adb_path = $AdbPath
    adb_serial = $Serial
    generated_config = $GeneratedConfig
} | ConvertTo-Json -Depth 5
