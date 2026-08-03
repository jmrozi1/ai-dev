$ErrorActionPreference = 'Stop'

$DefaultSuite = 'unit'
$UnitModules = @(
    'tests.test_script_entrypoints'
    'tests.test_bootstrap'
    'tests.test_bootstrap_cli'
)
$BootstrapModules = @(
    'tests.test_bootstrap'
    'tests.test_bootstrap_cli'
)
$IntegrationDiscoveryArgs = @('discover', '-s', 'tests', '-p', 'test_*.py')

$scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$repositoryRoot = Split-Path -Parent $scriptDirectory
$flowShellDirDefault = Join-Path $repositoryRoot 'tests/shell/flow'
$bootstrapShellDirDefault = Join-Path $repositoryRoot 'tests/shell/bootstrap'
$flowShellDir = if ($env:AI_DEV_TEST_FLOW_DIR) { $env:AI_DEV_TEST_FLOW_DIR } else { $flowShellDirDefault }
$bootstrapShellDir = if ($env:AI_DEV_TEST_BOOTSTRAP_DIR) { $env:AI_DEV_TEST_BOOTSTRAP_DIR } else { $bootstrapShellDirDefault }

. (Join-Path $repositoryRoot 'tools\bootstrap\PythonSelection.ps1')

function Get-ShellTestScripts {
    param([Parameter(Mandatory = $true)][string]$Directory)

    if (-not (Test-Path -LiteralPath $Directory -PathType Container)) {
        return @()
    }

    return @(Get-ChildItem -LiteralPath $Directory -Filter 'test-*.sh' -File | Sort-Object Name | ForEach-Object { $_.FullName })
}

function To-DisplayPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    $repoWithSeparator = "$repositoryRoot/"
    $normalized = $Path -replace '\\', '/'
    if ($normalized.StartsWith($repoWithSeparator)) {
        return $normalized.Substring($repoWithSeparator.Length)
    }
    return $Path
}

function Show-ShellSuiteListing {
    param(
        [Parameter(Mandatory = $true)][string]$SuiteName,
        [Parameter(Mandatory = $true)][string]$Directory,
        [Parameter(Mandatory = $true)][string[]]$Scripts
    )

    "$SuiteName:" | Write-Output
    "  purpose: Run shell suites discovered under $Directory" | Write-Output
    "  shell-dir: $Directory" | Write-Output
    if ($Scripts.Count -eq 0) {
        '  shell-tests: none discovered' | Write-Output
        return
    }
    '  shell-tests:' | Write-Output
    foreach ($scriptPath in $Scripts) {
        "    - $(To-DisplayPath -Path $scriptPath)" | Write-Output
    }
}

function Show-Help {
    @'
Usage: scripts/test.ps1 [suite] [options] [-- unittest-args]

Run repository test suites through one canonical dispatcher.

Suites:
  unit         Fast Python unit coverage (default).
  bootstrap    Bootstrap-focused Python tests and shell bootstrap suites.
  flow         Shell lifecycle suites under tests/shell/flow/.
  integration  Broader cross-component Python discovery suite.
  all          Complete Python + shell matrix.

Options:
  -h, --help   Show this help and exit.
  --list       Show suite mapping and underlying tests/directories.
  --all        Alias for suite "all".

Default behavior:
  Runs the "unit" suite:
    tests.test_script_entrypoints
    tests.test_bootstrap
    tests.test_bootstrap_cli

Forwarded unittest args:
  Use -- to pass arguments to Python unittest invocations.
  Supported suites for forwarded unittest args: unit, bootstrap, integration, all.
  The flow suite rejects forwarded unittest args because it only runs shell suites.

Shell-suite policy:
  On PowerShell, shell suites are executed with bash when available.
  If bash is unavailable, shell suites are explicitly reported as skipped.

Minimum Python version: 3.8

Examples:
  .\scripts\test.ps1
  .\scripts\test.ps1 bootstrap
  .\scripts\test.ps1 integration -- -k review
  .\scripts\test.ps1 all -- -k namespace
'@ | Write-Output
}

function Show-List {
    $flowShellTests = Get-ShellTestScripts -Directory $flowShellDir
    $bootstrapShellTests = Get-ShellTestScripts -Directory $bootstrapShellDir

    'unit:' | Write-Output
    '  purpose: Fast Python unit coverage without shell lifecycle suites' | Write-Output
    '  python-modules:' | Write-Output
    foreach ($module in $UnitModules) {
        "    - $module" | Write-Output
    }

    'bootstrap:' | Write-Output
    '  purpose: Bootstrap-focused Python and shell bootstrap coverage' | Write-Output
    '  python-modules:' | Write-Output
    foreach ($module in $BootstrapModules) {
        "    - $module" | Write-Output
    }
    Show-ShellSuiteListing -SuiteName '  bootstrap-shell' -Directory $bootstrapShellDir -Scripts $bootstrapShellTests

    Show-ShellSuiteListing -SuiteName 'flow' -Directory $flowShellDir -Scripts $flowShellTests

    'integration:' | Write-Output
    '  purpose: Broader cross-component Python integration discovery suite' | Write-Output
    '  python-discovery: python -m unittest discover -s tests -p test_*.py' | Write-Output

    'all:' | Write-Output
    '  purpose: Complete Python and shell matrix (integration + bootstrap shell + flow shell)' | Write-Output
    '  python-discovery: python -m unittest discover -s tests -p test_*.py' | Write-Output
    Show-ShellSuiteListing -SuiteName '  all-bootstrap-shell' -Directory $bootstrapShellDir -Scripts $bootstrapShellTests
    Show-ShellSuiteListing -SuiteName '  all-flow-shell' -Directory $flowShellDir -Scripts $flowShellTests
}

function Run-PythonModules {
    param(
        [Parameter(Mandatory = $true)][string]$PythonExecutable,
        [Parameter(Mandatory = $true)][string[]]$Modules,
        [Parameter(Mandatory = $true)][string[]]$ForwardArgs
    )

    & $PythonExecutable -m unittest @Modules @ForwardArgs
    if ($null -eq $LASTEXITCODE) { return 0 }
    return ([int]$LASTEXITCODE)
}

function Run-PythonDiscovery {
    param(
        [Parameter(Mandatory = $true)][string]$PythonExecutable,
        [Parameter(Mandatory = $true)][string[]]$ForwardArgs
    )

    & $PythonExecutable -m unittest @IntegrationDiscoveryArgs @ForwardArgs
    if ($null -eq $LASTEXITCODE) { return 0 }
    return ([int]$LASTEXITCODE)
}

function Run-ShellSuite {
    param(
        [Parameter(Mandatory = $true)][string]$SuiteLabel,
        [Parameter(Mandatory = $true)][string]$Directory
    )

    $scripts = Get-ShellTestScripts -Directory $Directory
    if ($scripts.Count -eq 0) {
        Write-Output "$SuiteLabel: no shell tests discovered under $Directory"
        return 0
    }

    $bash = Get-Command bash -ErrorAction SilentlyContinue
    if ($null -eq $bash) {
        [Console]::Error.WriteLine("SKIP [$SuiteLabel]: bash is not available; shell suites under $Directory were not executed.")
        return 0
    }

    foreach ($scriptPath in $scripts) {
        Write-Output "[$SuiteLabel] bash $(To-DisplayPath -Path $scriptPath)"
        & $bash.Source $scriptPath
        if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) {
            return ([int]$LASTEXITCODE)
        }
    }

    return 0
}

$suite = $null
$forwardArgs = New-Object System.Collections.Generic.List[string]
$passThroughUnittestArgs = $false

for ($index = 0; $index -lt $args.Count; $index++) {
    $arg = [string]$args[$index]

    if ($passThroughUnittestArgs) {
        $null = $forwardArgs.Add($arg)
        continue
    }

    switch ($arg) {
        '-h' {
            Show-Help
            exit 0
        }
        '--help' {
            Show-Help
            exit 0
        }
        '--list' {
            Show-List
            exit 0
        }
        '--all' {
            if ($null -ne $suite -and $suite -ne 'all') {
                [Console]::Error.WriteLine("test.ps1: multiple suites specified ($suite and all).")
                Show-Help | Out-String | ForEach-Object { [Console]::Error.Write($_) }
                exit 2
            }
            $suite = 'all'
        }
        '--' {
            $passThroughUnittestArgs = $true
        }
        default {
            if ($arg.StartsWith('-')) {
                [Console]::Error.WriteLine("test.ps1: unsupported option: $arg")
                Show-Help | Out-String | ForEach-Object { [Console]::Error.Write($_) }
                exit 2
            }
            if ($null -ne $suite) {
                [Console]::Error.WriteLine("test.ps1: multiple suites specified ($suite and $arg).")
                Show-Help | Out-String | ForEach-Object { [Console]::Error.Write($_) }
                exit 2
            }
            $suite = $arg
        }
    }
}

if ($null -eq $suite) {
    $suite = $DefaultSuite
}

if ($suite -notin @('unit', 'bootstrap', 'flow', 'integration', 'all')) {
    [Console]::Error.WriteLine("test.ps1: unknown suite: $suite")
    Show-Help | Out-String | ForEach-Object { [Console]::Error.Write($_) }
    exit 2
}

if ($suite -eq 'flow' -and $forwardArgs.Count -gt 0) {
    [Console]::Error.WriteLine('test.ps1: suite "flow" does not accept unittest args after --.')
    Show-Help | Out-String | ForEach-Object { [Console]::Error.Write($_) }
    exit 2
}

$pythonExecutable = $null
if ($suite -ne 'flow') {
    $pythonExecutable = Resolve-AiDevPythonExecutable -CallerName 'test.ps1'
}

$exitCode = 0

switch ($suite) {
    'unit' {
        $exitCode = Run-PythonModules -PythonExecutable $pythonExecutable -Modules $UnitModules -ForwardArgs $forwardArgs.ToArray()
    }
    'bootstrap' {
        $exitCode = Run-PythonModules -PythonExecutable $pythonExecutable -Modules $BootstrapModules -ForwardArgs $forwardArgs.ToArray()
        if ($exitCode -eq 0) {
            $exitCode = Run-ShellSuite -SuiteLabel 'bootstrap' -Directory $bootstrapShellDir
        }
    }
    'flow' {
        $exitCode = Run-ShellSuite -SuiteLabel 'flow' -Directory $flowShellDir
    }
    'integration' {
        $exitCode = Run-PythonDiscovery -PythonExecutable $pythonExecutable -ForwardArgs $forwardArgs.ToArray()
    }
    'all' {
        $exitCode = Run-PythonDiscovery -PythonExecutable $pythonExecutable -ForwardArgs $forwardArgs.ToArray()
        if ($exitCode -eq 0) {
            $exitCode = Run-ShellSuite -SuiteLabel 'bootstrap' -Directory $bootstrapShellDir
        }
        if ($exitCode -eq 0) {
            $exitCode = Run-ShellSuite -SuiteLabel 'flow' -Directory $flowShellDir
        }
    }
}

exit ([int]$exitCode)
