$previousErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = 'Stop'

try {
    $scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
    $repositoryRoot = Split-Path -Parent $scriptDirectory
    $flowLauncherPath = Join-Path $repositoryRoot 'scripts\flow.ps1'
    $skipUserPathUpdate = $env:FLOW_BOOTSTRAP_SKIP_USER_PATH_UPDATE -eq '1'
    $bootstrapHome = $env:FLOW_BOOTSTRAP_HOME
    if ([string]::IsNullOrWhiteSpace($bootstrapHome)) {
        $bootstrapHome = $HOME
    }

    if (-not (Test-Path -LiteralPath $flowLauncherPath -PathType Leaf)) {
        throw "required launcher not found at: $flowLauncherPath"
    }

    $userBinDirectory = Join-Path $bootstrapHome '.local\bin'
    New-Item -ItemType Directory -Path $userBinDirectory -Force | Out-Null

    $shimPath = Join-Path $userBinDirectory 'flow.ps1'
    $escapedLauncherPath = $flowLauncherPath.Replace("'", "''")

    $shimContent = @"
`$launcher = '$escapedLauncherPath'
`$previousFlowCallerCwd = `$env:FLOW_CALLER_CWD
`$env:FLOW_CALLER_CWD = (Get-Location).ProviderPath

try {
    & `$launcher @args
    exit `$LASTEXITCODE
}
finally {
    if (`$null -eq `$previousFlowCallerCwd) {
        Remove-Item Env:FLOW_CALLER_CWD -ErrorAction SilentlyContinue
    }
    else {
        `$env:FLOW_CALLER_CWD = `$previousFlowCallerCwd
    }
}
"@

    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($shimPath, $shimContent, $utf8NoBom)

function Normalize-PathEntry {
    param(
        [string]$Value
    )

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return ''
    }

    $normalized = $Value.Trim()

    if ($normalized.StartsWith('"') -and $normalized.EndsWith('"')) {
        $normalized = $normalized.Substring(1, $normalized.Length - 2)
    }

    return $normalized.TrimEnd('\', '/')
}

function Test-PathContainsEntry {
    param(
        [string]$PathValue,
        [string]$Entry
    )

    if ([string]::IsNullOrWhiteSpace($PathValue)) {
        return $false
    }

    $target = Normalize-PathEntry -Value $Entry
    foreach ($candidate in $PathValue.Split(';')) {
        if ((Normalize-PathEntry -Value $candidate) -ieq $target) {
            return $true
        }
    }

    return $false
}

    $userPathChanged = $false
    $processPathChanged = $false
    $resolvedUserBin = [System.IO.Path]::GetFullPath($userBinDirectory)

    if (-not $skipUserPathUpdate) {
        $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
        if (-not (Test-PathContainsEntry -PathValue $userPath -Entry $resolvedUserBin)) {
            if ([string]::IsNullOrWhiteSpace($userPath)) {
                $userPath = $resolvedUserBin
            }
            else {
                $userPath = "$userPath;$resolvedUserBin"
            }

            [Environment]::SetEnvironmentVariable('Path', $userPath, 'User')
            $userPathChanged = $true
        }
    }

    if (-not (Test-PathContainsEntry -PathValue $env:Path -Entry $resolvedUserBin)) {
        if ([string]::IsNullOrWhiteSpace($env:Path)) {
            $env:Path = $resolvedUserBin
        }
        else {
            $env:Path = "$env:Path;$resolvedUserBin"
        }

        $processPathChanged = $true
    }

    Get-Command flow -ErrorAction Stop | Out-Null

    $pathChanged = $userPathChanged -or $processPathChanged

    Write-Output "Shim path: $shimPath"
    if ($pathChanged) {
        Write-Output 'PATH changed: yes'
    }
    else {
        Write-Output 'PATH changed: no'
    }

    Write-Output 'Run: flow --help'
}
catch {
    [Console]::Error.WriteLine("flow bootstrap: $($_.Exception.Message)")
    exit 1
}
finally {
    $ErrorActionPreference = $previousErrorActionPreference
}