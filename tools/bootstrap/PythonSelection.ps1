$script:AiDevMinimumPythonVersion = [Version]'3.8.0'

function Get-AiDevMinimumPythonVersion {
    return $script:AiDevMinimumPythonVersion
}

function Resolve-AiDevPythonCandidatePath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Candidate
    )

    if ([string]::IsNullOrWhiteSpace($Candidate)) {
        return $null
    }

    if (Test-Path -LiteralPath $Candidate -PathType Leaf) {
        return (Resolve-Path -LiteralPath $Candidate).ProviderPath
    }

    $command = Get-Command $Candidate -ErrorAction SilentlyContinue
    if ($null -eq $command) {
        return $null
    }

    return $command.Source
}

function Get-AiDevPythonVersion {
    param(
        [Parameter(Mandatory = $true)]
        [string]$PythonPath
    )

    try {
        $output = & $PythonPath -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}')" 2>&1
        $exitCode = if ($null -eq $LASTEXITCODE) { 0 } else { [int]$LASTEXITCODE }
    }
    catch {
        return [PSCustomObject]@{
            Ok = $false
            Version = $null
            Detail = $_.Exception.Message
        }
    }

    if ($exitCode -ne 0) {
        $text = ($output | Out-String).Trim()
        return [PSCustomObject]@{
            Ok = $false
            Version = $null
            Detail = "probe failed: $text"
        }
    }

    $line = (($output | Out-String).Trim() -split "`r?`n")[0].Trim()
    if ($line -notmatch '^(\d+)\.(\d+)\.(\d+)$') {
        return [PSCustomObject]@{
            Ok = $false
            Version = $null
            Detail = "unrecognized version output: $line"
        }
    }

    return [PSCustomObject]@{
        Ok = $true
        Version = [Version]::new([int]$Matches[1], [int]$Matches[2], [int]$Matches[3])
        Detail = $line
    }
}

function Resolve-AiDevPythonExecutable {
    param(
        [Parameter(Mandatory = $true)]
        [string]$CallerName
    )

    $minimumVersion = Get-AiDevMinimumPythonVersion
    $rejected = New-Object System.Collections.Generic.List[string]
    $seen = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::OrdinalIgnoreCase)

    function Add-Rejected {
        param([string]$Line)
        [void]$rejected.Add($Line)
    }

    function Try-Candidate {
        param(
            [string]$Label,
            [string]$ResolvedPath,
            [bool]$Explicit
        )

        if ([string]::IsNullOrWhiteSpace($ResolvedPath)) {
            if ($Explicit) {
                throw "$CallerName: AI_DEV_PYTHON is set but was not found or not executable: $env:AI_DEV_PYTHON"
            }
            return $null
        }

        if ($seen.Contains($ResolvedPath)) {
            return $null
        }
        [void]$seen.Add($ResolvedPath)

        $probe = Get-AiDevPythonVersion -PythonPath $ResolvedPath
        if (-not $probe.Ok) {
            Add-Rejected "$Label -> $ResolvedPath ($($probe.Detail))"
            if ($Explicit) {
                throw "$CallerName: AI_DEV_PYTHON could not be validated: $Label -> $ResolvedPath ($($probe.Detail))"
            }
            return $null
        }

        if ($probe.Version -ge $minimumVersion) {
            return $ResolvedPath
        }

        Add-Rejected "$Label -> $ResolvedPath (version $($probe.Detail))"
        if ($Explicit) {
            throw "$CallerName: AI_DEV_PYTHON points to unsupported Python version $($probe.Detail). Minimum supported version is $minimumVersion."
        }
        return $null
    }

    if (-not [string]::IsNullOrWhiteSpace($env:AI_DEV_PYTHON)) {
        $explicitPath = Resolve-AiDevPythonCandidatePath -Candidate $env:AI_DEV_PYTHON
        $explicitSelected = Try-Candidate -Label "AI_DEV_PYTHON=$env:AI_DEV_PYTHON" -ResolvedPath $explicitPath -Explicit $true
        if (-not [string]::IsNullOrWhiteSpace($explicitSelected)) {
            return $explicitSelected
        }
        throw "$CallerName: AI_DEV_PYTHON must point to Python >= $minimumVersion."
    }

    $pyCommand = Get-Command py -ErrorAction SilentlyContinue
    if ($null -ne $pyCommand) {
        foreach ($tag in @('3.13', '3.12', '3.11', '3.10', '3.9', '3.8')) {
            try {
                $resolved = & py "-$tag" -c "import sys; print(sys.executable)" 2>$null
                $exitCode = if ($null -eq $LASTEXITCODE) { 0 } else { [int]$LASTEXITCODE }
            }
            catch {
                $exitCode = 1
                $resolved = $null
            }

            if ($exitCode -ne 0) {
                continue
            }

            $resolvedText = ($resolved | Out-String).Trim()
            if ([string]::IsNullOrWhiteSpace($resolvedText)) {
                continue
            }

            $resolvedPath = Resolve-AiDevPythonCandidatePath -Candidate $resolvedText
            $selected = Try-Candidate -Label "py -$tag" -ResolvedPath $resolvedPath -Explicit $false
            if (-not [string]::IsNullOrWhiteSpace($selected)) {
                return $selected
            }
        }
    }

    foreach ($candidate in @('python3.13', 'python3.12', 'python3.11', 'python3.10', 'python3.9', 'python3.8', 'python3')) {
        $resolvedPath = Resolve-AiDevPythonCandidatePath -Candidate $candidate
        $selected = Try-Candidate -Label $candidate -ResolvedPath $resolvedPath -Explicit $false
        if (-not [string]::IsNullOrWhiteSpace($selected)) {
            return $selected
        }
    }

    $pythonPath = Resolve-AiDevPythonCandidatePath -Candidate 'python'
    $pythonSelected = Try-Candidate -Label 'python' -ResolvedPath $pythonPath -Explicit $false
    if (-not [string]::IsNullOrWhiteSpace($pythonSelected)) {
        return $pythonSelected
    }

    $details = @("$CallerName: No compatible Python interpreter found. Minimum supported version is $minimumVersion.")
    if ($rejected.Count -gt 0) {
        $details += "$CallerName: Discovered but rejected interpreters:"
        foreach ($line in $rejected) {
            $details += "  - $line"
        }
    }
    $details += "$CallerName: Set AI_DEV_PYTHON to a compatible interpreter path to continue."

    throw ($details -join [Environment]::NewLine)
}
