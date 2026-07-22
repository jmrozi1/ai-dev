$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$coreScript = Join-Path $root "scripts\build-vscode-plugin.cjs"
$out = Join-Path $root "out.txt"

try {
    if (-not (Test-Path $coreScript -PathType Leaf)) {
        throw "Shared plugin build script not found: $coreScript"
    }

    $node = Get-Command node.exe -ErrorAction SilentlyContinue

    if (-not $node) {
        throw "node.exe was not found on PATH."
    }

    & $node.Source $coreScript --install

    if ($LASTEXITCODE -ne 0) {
        throw "Shared plugin build failed with exit code $LASTEXITCODE."
    }
}
catch {
    Write-Host
    Write-Host "AI Dev plugin build or installation failed."
    Write-Host "Log: $out"
    Write-Host $_.Exception.Message
    exit 1
}
