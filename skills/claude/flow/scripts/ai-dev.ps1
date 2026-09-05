$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = (Resolve-Path (Join-Path $scriptDir '..\..\..\..')).ProviderPath
. (Join-Path $repoRoot 'tools\bootstrap\PythonSelection.ps1')
$pythonExecutable = Resolve-AiDevPythonExecutable -CallerName 'ai-dev'
& $pythonExecutable (Join-Path $repoRoot 'tools\claude\ai-dev-entry.py') @args
exit $LASTEXITCODE
