param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$flowHelper = Join-Path (Resolve-Path (Join-Path $scriptDir '..\..\flow\scripts')).ProviderPath 'flow-report.ps1'
if (-not (Test-Path -Path $flowHelper -PathType Leaf)) {
    throw "error: missing sibling Flow helper at $flowHelper; repair the installed skill layout"
}

& $flowHelper @Arguments
exit $LASTEXITCODE
