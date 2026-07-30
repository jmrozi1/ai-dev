$previousErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = 'Stop'
$bootstrapExitCode = 1

try {
    $scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
    $repositoryRoot = Split-Path -Parent $scriptDirectory

    function Resolve-PythonExecutable {
        if (-not [string]::IsNullOrWhiteSpace($env:AI_DEV_PYTHON)) {
            return $env:AI_DEV_PYTHON
        }

        $pyCommand = Get-Command py -ErrorAction SilentlyContinue
        if ($null -ne $pyCommand) {
            $resolved = & py -3 -c "import sys; print(sys.executable)" 2>$null
            if (-not [string]::IsNullOrWhiteSpace($resolved)) {
                return $resolved.Trim()
            }
        }

        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if ($null -ne $pythonCommand) {
            return $pythonCommand.Source
        }

        throw 'Python 3 was not found. Set AI_DEV_PYTHON or install python.'
    }

    $pythonExecutable = Resolve-PythonExecutable

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
            --repo-root $repositoryRoot `
            --python $pythonExecutable `
            --command-name ai-dev
        if ($null -eq $LASTEXITCODE) {
            $bootstrapExitCode = 0
        }
        else {
            $bootstrapExitCode = [int]$LASTEXITCODE
        }
    }
    finally {
        $env:PYTHONPATH = $previousPythonPath
    }
}
catch {
    [Console]::Error.WriteLine("bootstrap-ai-dev.ps1: $($_.Exception.Message)")
    $bootstrapExitCode = 1
}
finally {
    $ErrorActionPreference = $previousErrorActionPreference
    $global:LASTEXITCODE = $bootstrapExitCode
}

return
