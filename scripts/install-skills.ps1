param(
    [Alias('f')]
    [switch]$Force,
    [Alias('h')]
    [switch]$Help
)

$ErrorActionPreference = 'Stop'

function Show-Help {
    @'
Usage: scripts/install-skills.ps1 [-Force]

Install repository skills into ~/.agents/skills.

Options:
  -Force, -f  Replace matching installed skills without prompting.
  -Help, -h   Show this help and exit.

Unrelated skills already installed under ~/.agents/skills are left unchanged.
'@ | Write-Output
}

if ($Help) {
    Show-Help
    exit 0
}

$scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$repositoryRoot = Split-Path -Parent $scriptDirectory
$sourceRoot = Join-Path $repositoryRoot 'skills'
$destinationRoot = Join-Path $HOME '.agents\skills'

if (-not (Test-Path -LiteralPath $sourceRoot -PathType Container)) {
    throw "Skills directory not found: $sourceRoot"
}

New-Item -ItemType Directory -Path $destinationRoot -Force | Out-Null

$skillFiles = Get-ChildItem -LiteralPath $sourceRoot -Filter 'SKILL.md' -File -Recurse | Sort-Object FullName
$installed = 0
$skipped = 0

foreach ($skillFile in $skillFiles) {
    $sourceDirectory = $skillFile.Directory.FullName
    $relativePath = [System.IO.Path]::GetRelativePath($sourceRoot, $sourceDirectory)
    $destinationDirectory = Join-Path $destinationRoot $relativePath

    if ((Test-Path -LiteralPath $destinationDirectory) -and -not $Force) {
        $response = Read-Host "Replace existing skill '$relativePath'? [y/N]"
        if ($response -notmatch '^(?i:y|yes)$') {
            Write-Output "Skipped $relativePath"
            $skipped++
            continue
        }
    }

    if (Test-Path -LiteralPath $destinationDirectory) {
        Remove-Item -LiteralPath $destinationDirectory -Recurse -Force
    }

    $destinationParent = Split-Path -Parent $destinationDirectory
    New-Item -ItemType Directory -Path $destinationParent -Force | Out-Null
    Copy-Item -LiteralPath $sourceDirectory -Destination $destinationDirectory -Recurse -Force

    Write-Output "Installed $relativePath"
    $installed++
}

Write-Output "Skill installation complete: $installed installed, $skipped skipped."
