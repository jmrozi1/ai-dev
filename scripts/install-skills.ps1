param(
    [Alias('h')]
    [switch]$Help
)

$ErrorActionPreference = 'Stop'

function Show-Help {
    @'
Usage: scripts/install-skills.ps1 [skill-installer-options]

Install repository Copilot skills discovered from:
  skills/*/SKILL.md

Default destination is:
    ~/.agents/skills

Options:
  -Help, -h  Show this help and exit.

Any additional options are forwarded to:
  python -m ai_dev_flow.skill_installation

Examples:
  .\scripts\install-skills.ps1
    .\scripts\install-skills.ps1 --destination-root "$HOME\.agents\skills"
'@ | Write-Output
}

if ($Help) {
    Show-Help
    exit 0
}

$forwardedArguments = @()
foreach ($argument in $args) {
    if ($argument -eq '-h' -or $argument -eq '--help') {
        Show-Help
        exit 0
    }
    $forwardedArguments += $argument
}

$scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$repositoryRoot = Split-Path -Parent $scriptDirectory
. (Join-Path $repositoryRoot 'tools\bootstrap\PythonSelection.ps1')
$pythonExecutable = Resolve-AiDevPythonExecutable -CallerName 'install-skills.ps1'

$previousPythonPath = $env:PYTHONPATH
if ([string]::IsNullOrWhiteSpace($previousPythonPath)) {
    $env:PYTHONPATH = $repositoryRoot
}
else {
    $env:PYTHONPATH = "$repositoryRoot$([System.IO.Path]::PathSeparator)$previousPythonPath"
}

try {
    & $pythonExecutable -m ai_dev_flow.skill_installation `
        --repo-root $repositoryRoot `
        @forwardedArguments
    if ($null -eq $LASTEXITCODE) {
        exit 0
    }
    exit ([int]$LASTEXITCODE)
}
catch {
    [Console]::Error.WriteLine("install-skills.ps1: $($_.Exception.Message)")
    exit 1
}
finally {
    $env:PYTHONPATH = $previousPythonPath
}
