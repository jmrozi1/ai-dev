$ErrorActionPreference = 'Stop'

function Show-Help {
    @'
Usage: scripts/install.ps1 [bootstrap-options]

Safely remove AI Dev-managed legacy Flow launchers.

This wrapper calls:
    python -m ai_dev_flow.bootstrap --platform windows

Options:
  -h, --help  Show this help and exit.

`--home <path>` cleans the ownership record under a different home.
Common examples:
  .\scripts\install.ps1
    .\scripts\install.ps1 --home $HOME

Minimum Python version: 3.8

Normal Flow execution uses the installed Copilot skill package, not PATH commands.
'@ | Write-Output
}

foreach ($arg in $args) {
    if ($arg -eq '-h' -or $arg -eq '--help') {
        Show-Help
        exit 0
    }
}

$scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$repositoryRoot = Split-Path -Parent $scriptDirectory
. (Join-Path $repositoryRoot 'tools\bootstrap\PythonSelection.ps1')
$pythonExecutable = Resolve-AiDevPythonExecutable -CallerName 'install.ps1'

$previousPythonPath = $env:PYTHONPATH
if ([string]::IsNullOrWhiteSpace($previousPythonPath)) {
    $env:PYTHONPATH = $repositoryRoot
}
else {
    $env:PYTHONPATH = "$repositoryRoot$([System.IO.Path]::PathSeparator)$previousPythonPath"
}

try {
    & $pythonExecutable -m ai_dev_flow.bootstrap `
        --platform windows `
        @args
    if ($null -eq $LASTEXITCODE) {
        exit 0
    }
    exit ([int]$LASTEXITCODE)
}
catch {
    [Console]::Error.WriteLine("install.ps1: $($_.Exception.Message)")
    exit 1
}
finally {
    $env:PYTHONPATH = $previousPythonPath
}
