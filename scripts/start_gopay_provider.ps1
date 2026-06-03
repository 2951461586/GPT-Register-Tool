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

function Set-JsonProperty($Object, [string]$Name, $Value) {
    if ($null -eq $Object.PSObject.Properties[$Name]) {
        $Object | Add-Member -NotePropertyName $Name -NotePropertyValue $Value
    } else {
        $Object.$Name = $Value
    }
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
$WaRebindEnabled = $false
if ($null -ne $WaRebind) {
    $WaEnabledText = ([string](First-Value @($WaRebind.enabled) "false")).Trim().ToLowerInvariant()
    $WaRebindEnabled = @("true", "1", "yes", "on") -contains $WaEnabledText
}
$GoPayAppAddr = ""
$GoPayAppHost = ""
$GoPayAppPort = 0
if ($WaRebindEnabled) {
    $GoPayAppAddr = First-Value @($WaRebind.gopay_app_service_addr, $GoPay.gopay_app_service_addr) ""
    if (![string]::IsNullOrWhiteSpace($GoPayAppAddr) -and $GoPayAppAddr.Contains(":")) {
        $GoPayAppHost, $GoPayAppPortText = $GoPayAppAddr.Split(":", 2)
        $GoPayAppPort = [int]$GoPayAppPortText
    }
}

$EmulatorExe = Resolve-ProjectPath (First-Value @($GoPay.emulator_exe) "D:\Program Files\Netease\MuMuPlayer\nx_main\MuMuNxMain.exe")
$AdbPath = Resolve-ProjectPath (First-Value @($GoPay.adb_path) "D:\Program Files\Netease\MuMuPlayer\nx_main\adb.exe")
$Serial = First-Value @($GoPay.adb_serial) "emulator-5554"

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
$ProviderConfig.gopay.pin = First-Value @($GoPay.pin) "147258"
Set-JsonProperty $ProviderConfig.gopay "grpcurl_path" (First-Value @($GoPay.grpcurl_path, $GoPay.grpcurl) "grpcurl")
Set-JsonProperty $ProviderConfig.gopay "pure_xe_mode" (First-Value @($GoPay.pure_xe_mode) "enhanced")
Set-JsonProperty $ProviderConfig.gopay "pure_xe_resolution_key" (First-Value @($GoPay.pure_xe_resolution_key) "")
Set-JsonProperty $ProviderConfig.gopay "pure_xe_random_hex" (First-Value @($GoPay.pure_xe_random_hex) "")
Set-JsonProperty $ProviderConfig.gopay "pure_device_id" (First-Value @($GoPay.pure_device_id) "")
Set-JsonProperty $ProviderConfig.gopay "pure_x_m1" (First-Value @($GoPay.pure_x_m1) "")
Set-JsonProperty $ProviderConfig.gopay "pure_protocol_timeout_seconds" ([int](First-Value @($GoPay.pure_protocol_timeout_seconds) "35"))
Set-JsonProperty $ProviderConfig.gopay "pure_protocol_debug" (First-Value @($GoPay.pure_protocol_debug) "false")
if ($WaRebindEnabled -and ![string]::IsNullOrWhiteSpace($GoPayAppAddr)) {
    Set-JsonProperty $ProviderConfig.gopay "gopay_app_service_addr" $GoPayAppAddr
    Set-JsonProperty $ProviderConfig.gopay "gopay_app_service" (First-Value @($WaRebind.gopay_app_service, $GoPay.gopay_app_service) "gopay_app.GopayAppService")
    Set-JsonProperty $ProviderConfig.gopay "gopay_app_proto_import_path" (First-Value @($WaRebind.gopay_app_proto_import_path, $GoPay.gopay_app_proto_import_path) "services\gopay-app\proto")
    Set-JsonProperty $ProviderConfig.gopay "gopay_app_proto_path" (First-Value @($WaRebind.gopay_app_proto_path, $GoPay.gopay_app_proto_path) "services\gopay-app\proto\gopay_app.proto")
    Set-JsonProperty $ProviderConfig.gopay "gopay_app_timeout_seconds" ([int](First-Value @($WaRebind.timeout_seconds, $GoPay.gopay_app_timeout_seconds, $GoPay.provider_timeout_seconds) "600"))
}
if ($null -eq $ProviderConfig.gopay.otp) {
    $ProviderConfig.gopay | Add-Member -NotePropertyName otp -NotePropertyValue ([pscustomobject]@{})
}
if ($null -eq $ProviderConfig.gopay.unlink) {
    $ProviderConfig.gopay | Add-Member -NotePropertyName unlink -NotePropertyValue ([pscustomobject]@{})
}
$RootPhoneReuse = $Config.phone_reuse
$RootSmsBower = $null
if ($null -ne $RootPhoneReuse) { $RootSmsBower = $RootPhoneReuse.smsbower }
$GoPayOtp = $GoPay.otp
$GoPayOtpSmsBower = $null
if ($null -ne $GoPayOtp) { $GoPayOtpSmsBower = $GoPayOtp.smsbower }
$OtpSource = (First-Value @($GoPay.otp_source, $GoPayOtp.source, $ProviderConfig.gopay.otp.source) "adb").ToLowerInvariant()
$ProviderConfig.gopay.otp.source = $OtpSource
if ($OtpSource -eq "sms_bower") { $OtpSource = "smsbower"; $ProviderConfig.gopay.otp.source = "smsbower" }
if ($OtpSource -eq "smsbower") {
    $SmsBower = if ($null -ne $GoPayOtpSmsBower) { $GoPayOtpSmsBower } else { $RootSmsBower }
    if ($null -eq $SmsBower) {
        throw "GoPay otp_source=smsbower requires phone_reuse.smsbower or gopay.otp.smsbower"
    }
    $SmsBowerService = First-Value @($GoPayOtpSmsBower.service, $SmsBower.gopay_service) ""
    $SmsBowerCountry = First-Value @($GoPayOtpSmsBower.country, $SmsBower.gopay_country) ""
    if ([string]::IsNullOrWhiteSpace($SmsBowerService) -or [string]::IsNullOrWhiteSpace($SmsBowerCountry)) {
        throw "GoPay otp_source=smsbower requires gopay.otp.smsbower.service/country or phone_reuse.smsbower.gopay_service/gopay_country"
    }
    $SmsBowerMinPrice = First-Value @($GoPayOtpSmsBower.min_price, $RootSmsBower.gopay_min_price, $SmsBower.min_price) ""
    $SmsBowerMaxPrice = First-Value @($GoPayOtpSmsBower.max_price, $RootSmsBower.gopay_max_price, $SmsBower.max_price) ""
    $SmsBowerTargetPrice = First-Value @($GoPayOtpSmsBower.target_price, $RootSmsBower.gopay_target_price, $SmsBower.target_price) ""
    $SmsBowerApiKey = First-Value @($GoPayOtpSmsBower.api_key, $RootSmsBower.api_key, $SmsBower.api_key) "`$SMSBOWER_API_KEY"
    if ($SmsBowerApiKey -eq "`$SMSBOWER_API_KEY" -and $null -ne $RootSmsBower -and -not [string]::IsNullOrWhiteSpace([string]$RootSmsBower.api_key)) {
        $SmsBowerApiKey = [string]$RootSmsBower.api_key
    }
    $smsBowerPayload = [pscustomobject]@{
        api_key = $SmsBowerApiKey
        endpoint = First-Value @($SmsBower.endpoint) "https://smsbower.page/stubs/handler_api.php"
        service = $SmsBowerService
        country = $SmsBowerCountry
        min_price = $SmsBowerMinPrice
        max_price = $SmsBowerMaxPrice
        target_price = $SmsBowerTargetPrice
        sms_timeout = [int](First-Value @($SmsBower.sms_timeout) "120")
        sms_poll_interval = [int](First-Value @($SmsBower.sms_poll_interval) "5")
        register_account = First-Value @($GoPayOtpSmsBower.register_account, $SmsBower.gopay_register_account) "true"
        min_balance_rp = [int](First-Value @($GoPayOtpSmsBower.min_balance_rp, $GoPay.min_balance_rp, $SmsBower.gopay_min_balance_rp) "1")
        balance_wait_timeout_seconds = [int](First-Value @($GoPayOtpSmsBower.balance_wait_timeout_seconds, $GoPay.balance_wait_timeout_seconds, $SmsBower.gopay_balance_wait_timeout_seconds) "120")
        balance_poll_interval_seconds = [int](First-Value @($GoPayOtpSmsBower.balance_poll_interval_seconds, $GoPay.balance_poll_interval_seconds, $SmsBower.gopay_balance_poll_interval_seconds) "5")
    }
    if ($null -eq $ProviderConfig.gopay.otp.smsbower) {
        $ProviderConfig.gopay.otp | Add-Member -NotePropertyName smsbower -NotePropertyValue $smsBowerPayload
    } else {
        $ProviderConfig.gopay.otp.smsbower = $smsBowerPayload
    }
    $ProviderConfig.gopay.unlink.enabled = $false
} else {
    if (!(Test-Path -LiteralPath $AdbPath)) {
        throw "ADB not found: $AdbPath"
    }
    if ((Test-Path -LiteralPath $EmulatorExe) -and -not (Get-Process | Where-Object { $_.Path -eq $EmulatorExe })) {
        Start-Process -FilePath $EmulatorExe -WindowStyle Hidden
        Start-Sleep -Seconds 8
    }
}
$ProviderConfig.gopay.otp.adb_url = "http://$SidecarAddr"
$ProviderConfig.gopay.unlink.adb_url = "http://$SidecarAddr"
if ($OtpSource -ne "smsbower") {
    $ProviderConfig.gopay.unlink.enabled = $true
}
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

$GoPayAppListening = $false
if ($WaRebindEnabled -and $GoPayAppPort -gt 0) {
    $GoPayAppListening = [bool](Get-NetTCPConnection -LocalPort $GoPayAppPort -ErrorAction SilentlyContinue)
    if (-not $GoPayAppListening) {
        $GoPayAppExe = Resolve-ProjectPath (First-Value @($GoPay.gopay_app_exe) "runtime\gopay_provider\gopay-app-server.exe")
        if (Test-Path -LiteralPath $GoPayAppExe) {
            $env:GOPAY_APP_PORT = [string]$GoPayAppPort
            if (![string]::IsNullOrWhiteSpace($proxy)) {
                $env:GOPAY_DYNAMIC_EGRESS = $proxy
            }
            Start-Process -FilePath $GoPayAppExe -WindowStyle Hidden -WorkingDirectory $Root -RedirectStandardOutput (Join-Path $RuntimeDir "gopay_app_service.log") -RedirectStandardError (Join-Path $RuntimeDir "gopay_app_service.err.log")
        }
    }
}

$UsesAdbSidecar = $OtpSource -ne "smsbower"
$SidecarListening = $null
if ($UsesAdbSidecar) {
    $SidecarListening = Get-NetTCPConnection -LocalAddress $SidecarHost -LocalPort $SidecarPort -ErrorAction SilentlyContinue
}
if ($UsesAdbSidecar -and -not $SidecarListening) {
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
if ($UsesAdbSidecar) {
    $SidecarListening = Get-NetTCPConnection -LocalAddress $SidecarHost -LocalPort $SidecarPort -ErrorAction SilentlyContinue
}
if ($GoPayAppPort -gt 0) {
    $GoPayAppListening = [bool](Get-NetTCPConnection -LocalPort $GoPayAppPort -ErrorAction SilentlyContinue)
}
[pscustomobject]@{
    payment_service_addr = $PaymentAddr
    payment_service_listening = [bool]$PaymentListening
    otp_source = $OtpSource
    adb_sidecar_addr = $SidecarAddr
    adb_sidecar_listening = [bool]$SidecarListening
    gopay_app_service_addr = $GoPayAppAddr
    gopay_app_service_listening = [bool]$GoPayAppListening
    adb_path = $AdbPath
    adb_serial = $Serial
    generated_config = $GeneratedConfig
} | ConvertTo-Json -Depth 5
