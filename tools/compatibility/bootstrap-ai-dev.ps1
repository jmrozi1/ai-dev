$previousErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = 'Stop'
$bootstrapExitCode = 1

try {
    $scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
    $repositoryRoot = Split-Path -Parent (Split-Path -Parent $scriptDirectory)
    $installWrapper = Join-Path $repositoryRoot 'scripts\install.ps1'

    [Console]::Error.WriteLine('DEPRECATED: tools/compatibility/bootstrap-ai-dev.ps1 is deprecated.')
    [Console]::Error.WriteLine('Use scripts/install.ps1 instead (or run ai-dev apply).')

    & $installWrapper @args
    if ($null -eq $LASTEXITCODE) {
        $bootstrapExitCode = 0
    }
    else {
        $bootstrapExitCode = [int]$LASTEXITCODE
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
