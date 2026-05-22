param(
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"

$projectRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$repoRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$publishDir = [System.IO.Path]::GetFullPath((Join-Path $repoRoot "dist\net10"))

function Test-IsUnderPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Root
    )

    $fullPath = [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
    $fullRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd('\')
    return $fullPath.Equals($fullRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
        $fullPath.StartsWith($fullRoot + '\', [System.StringComparison]::OrdinalIgnoreCase)
}

$targets = @(
    (Join-Path $projectRoot "bin\Debug\net10.0-windows"),
    (Join-Path $projectRoot "bin\Release\net10.0-windows")
)

foreach ($target in $targets) {
    $resolved = [System.IO.Path]::GetFullPath($target)
    if (-not (Test-IsUnderPath -Path $resolved -Root (Join-Path $projectRoot "bin"))) {
        throw "Refusing to clean unexpected path outside SmsWorkbench\bin: $resolved"
    }
    if (Test-IsUnderPath -Path $resolved -Root $publishDir) {
        throw "Refusing to clean canonical publish directory: $resolved"
    }

    if (Test-Path -LiteralPath $resolved) {
        if ($WhatIf) {
            Write-Host "Would clean $resolved"
        }
        else {
            Remove-Item -LiteralPath $resolved -Recurse -Force
            Write-Host "Cleaned $resolved"
        }
    }
}

$emptyParents = @(
    (Join-Path $projectRoot "bin\Debug"),
    (Join-Path $projectRoot "bin\Release"),
    (Join-Path $projectRoot "bin")
)

foreach ($parent in $emptyParents) {
    $resolvedParent = [System.IO.Path]::GetFullPath($parent)
    if ((Test-Path -LiteralPath $resolvedParent) -and (Test-IsUnderPath -Path $resolvedParent -Root $projectRoot)) {
        $hasChildren = @(Get-ChildItem -LiteralPath $resolvedParent -Force).Count -gt 0
        if (-not $hasChildren) {
            if ($WhatIf) {
                Write-Host "Would remove empty $resolvedParent"
            }
            else {
                Remove-Item -LiteralPath $resolvedParent -Force
                Write-Host "Removed empty $resolvedParent"
            }
        }
    }
}

Write-Host "Kept canonical publish directory $publishDir"
