param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = (Resolve-Path (Join-Path $scriptDir '..\..\..\..')).ProviderPath
$previousPythonPath = $env:PYTHONPATH
if ([string]::IsNullOrWhiteSpace($previousPythonPath)) {
    $env:PYTHONPATH = $repoRoot
}
else {
    $env:PYTHONPATH = "$repoRoot$([System.IO.Path]::PathSeparator)$previousPythonPath"
}

. (Join-Path $repoRoot 'tools\bootstrap\PythonSelection.ps1')
$pythonExecutable = Resolve-AiDevPythonExecutable -CallerName 'ticket-status'
& $pythonExecutable -m ai_dev_flow.ticket_status @Arguments
exit $LASTEXITCODE