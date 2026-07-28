$scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$repositoryRoot = Split-Path -Parent $scriptDirectory

$commandName = [System.IO.Path]::GetFileNameWithoutExtension(
    $MyInvocation.MyCommand.Name
)

$previousPythonPath = $env:PYTHONPATH
$env:FLOW_COMMAND_NAME = $commandName

if ([string]::IsNullOrEmpty($previousPythonPath)) {
    $env:PYTHONPATH = $repositoryRoot
}
else {
    $env:PYTHONPATH = "$repositoryRoot$([System.IO.Path]::PathSeparator)$previousPythonPath"
}

$pythonExitCode = 0
$previousErrorActionPreference = $ErrorActionPreference
$callerWorkingDirectory = $env:FLOW_CALLER_CWD
if ([string]::IsNullOrWhiteSpace($callerWorkingDirectory)) {
    $callerWorkingDirectory = (Get-Location).ProviderPath
}

function ConvertTo-WindowsCommandLineArgument {
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$Value
    )

    if ($Value -notmatch '[\s\"]') {
        return $Value
    }

    $result = '"'
    $backslashCount = 0

    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') {
            $backslashCount += 1
            continue
        }

        if ($character -eq '"') {
            $result += ('\' * (($backslashCount * 2) + 1))
            $result += '"'
            $backslashCount = 0
            continue
        }

        if ($backslashCount -gt 0) {
            $result += ('\' * $backslashCount)
            $backslashCount = 0
        }

        $result += $character
    }

    if ($backslashCount -gt 0) {
        $result += ('\' * ($backslashCount * 2))
    }

    $result += '"'
    return $result
}

try {
    try {
        $ErrorActionPreference = 'Continue'

        $pythonArguments = @('-m', 'ai_dev_flow.cli') + $args
        $escapedArguments = $pythonArguments | ForEach-Object {
            ConvertTo-WindowsCommandLineArgument -Value ([string]$_)
        }

        $startInfo = New-Object System.Diagnostics.ProcessStartInfo
        $startInfo.FileName = 'python'
        $startInfo.Arguments = [string]::Join(' ', $escapedArguments)
        $startInfo.WorkingDirectory = $callerWorkingDirectory
        $startInfo.UseShellExecute = $false
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true

        $process = New-Object System.Diagnostics.Process
        $process.StartInfo = $startInfo
        $process.Start() | Out-Null

        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()

        $process.WaitForExit()
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        $pythonExitCode = $process.ExitCode

        if (-not [string]::IsNullOrEmpty($stdout)) {
            Write-Output $stdout
        }

        if (-not [string]::IsNullOrEmpty($stderr)) {
            [Console]::Error.Write($stderr)
        }
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
}
finally {
    $env:PYTHONPATH = $previousPythonPath
    Remove-Item Env:FLOW_COMMAND_NAME -ErrorAction SilentlyContinue
}

exit $pythonExitCode