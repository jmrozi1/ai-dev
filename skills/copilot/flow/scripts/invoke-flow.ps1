param(
    [Parameter(Mandatory = $true)]
    [string]$Command,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = (Resolve-Path (Join-Path $scriptDir '..\..\..\..')).ProviderPath
$env:FLOW_COMMAND_NAME = "flow-$Command"
$previousPythonPath = $env:PYTHONPATH
if ([string]::IsNullOrWhiteSpace($previousPythonPath)) {
    $env:PYTHONPATH = $repoRoot
}
else {
    $env:PYTHONPATH = "$repoRoot$([System.IO.Path]::PathSeparator)$previousPythonPath"
}

. (Join-Path $repoRoot 'tools\bootstrap\PythonSelection.ps1')
$pythonExecutable = Resolve-AiDevPythonExecutable -CallerName $env:FLOW_COMMAND_NAME
& $pythonExecutable -m ai_dev_flow.cli '__ai_dev_flow_exec__' $Command @Arguments
exit $LASTEXITCODE