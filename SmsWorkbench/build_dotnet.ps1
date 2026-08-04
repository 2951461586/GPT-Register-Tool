# ============================================================================
# SmsWorkbench 编译脚本 — 唯一支持的桌面程序编译入口
# ----------------------------------------------------------------------------
# 输出路径: <repo>/dist/net10/SmsWorkbench.exe
# 中间产物: SmsWorkbench/bin/{Debug,Release}/net10.0-windows  (发布后自动清理)
#
# ⚠ 禁止直接运行 `dotnet build`！直接 build 只输出中间产物且不会自动清理。
#    所有编译必须通过本脚本完成。
#
# 用法:
#   powershell -ExecutionPolicy Bypass -File .\SmsWorkbench\build_dotnet.ps1
# ============================================================================
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

# Remove binaries left by the retired local card-executor build. Keep the
# publish directory's runtime data; it is operator state, not build output.
$retiredExecutorArtifacts = @(
    "Microsoft.Web.WebView2.Core.dll",
    "Microsoft.Web.WebView2.Core.xml",
    "Microsoft.Web.WebView2.WinForms.dll",
    "Microsoft.Web.WebView2.WinForms.xml",
    "Microsoft.Web.WebView2.Wpf.dll",
    "Microsoft.Web.WebView2.Wpf.xml",
    "WebView2Loader.dll",
    "runtimes\win-x64\native\WebView2Loader.dll"
)
foreach ($relative in $retiredExecutorArtifacts) {
    $target = Join-Path $publishDir $relative
    if (Test-Path -LiteralPath $target -PathType Leaf) {
        Remove-Item -LiteralPath $target -Force
    }
}

& $dotnet publish $project `
    -c Release `
    -r win-x64 `
    --self-contained false `
    -p:PublishSingleFile=false `
    -o $publishDir

if ($LASTEXITCODE -ne 0) {
    throw "dotnet publish failed with exit code $LASTEXITCODE"
}

$cleanScript = Join-Path $PSScriptRoot "clean_dotnet_workspaces.ps1"
& $cleanScript

Write-Host "Published $publishDir\SmsWorkbench.exe"
