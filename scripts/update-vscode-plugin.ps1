$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$out = Join-Path $root "out.txt"

$core = Join-Path $root "ai-dev-core"
$extension = Join-Path $root "ai-dev-vscode"
$vendor = Join-Path $extension "vendor\ai-dev-core"
$artifacts = Join-Path $root "artifacts"

function Write-Log {
    param([string]$Text = "")
    $Text | Tee-Object -FilePath $out -Append
}

function Run-Native {
    param(
        [string]$Title,
        [scriptblock]$Command
    )

    Write-Log
    Write-Log "===== $Title ====="

    $global:LASTEXITCODE = 0

    # Windows PowerShell turns native stderr into PowerShell error records.
    # Temporarily allow those records through so warnings are logged, then
    # use the native process exit code to determine success or failure.
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"

    try {
        & $Command 2>&1 |
            ForEach-Object {
                $_.ToString() | Tee-Object -FilePath $out -Append
            }

        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }

    if ($null -eq $exitCode) {
        $exitCode = 0
    }

    Write-Log "ExitCode: $exitCode"

    if ($exitCode -ne 0) {
        throw "$Title failed with exit code $exitCode."
    }
}

function Require-Path {
    param(
        [string]$Path,
        [string]$Description
    )

    if (-not (Test-Path $Path)) {
        throw "$Description not found: $Path"
    }
}

Remove-Item $out -Force -ErrorAction SilentlyContinue

"===== build and install local AI Dev plugin =====" | Set-Content $out
Write-Log "Generated: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Write-Log "Repository: $root"
Write-Log "Source policy: use current local files; no Git pull"

try {
    Require-Path $core "ai-dev-core"
    Require-Path $extension "ai-dev-vscode"
    Require-Path (Join-Path $extension "package.json") "Extension package.json"

    $npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
    $npx = Get-Command npx.cmd -ErrorAction SilentlyContinue
    $node = Get-Command node.exe -ErrorAction SilentlyContinue
    $code = Get-Command code.cmd -ErrorAction SilentlyContinue

    if (-not $npm)  { throw "npm.cmd was not found." }
    if (-not $npx)  { throw "npx.cmd was not found." }
    if (-not $node) { throw "node.exe was not found." }
    if (-not $code) { throw "code.cmd was not found." }

    Write-Log
    Write-Log "===== local source status ====="
    git -C $root status --short --branch 2>&1 |
        ForEach-Object { Write-Log $_.ToString() }

    Write-Log
    Write-Log "===== clean previous VSIX artifacts ====="

    New-Item -ItemType Directory -Force -Path $artifacts | Out-Null

    Get-ChildItem $artifacts -File -Filter *.vsix -ErrorAction SilentlyContinue |
        Remove-Item -Force

    Write-Log "Previous VSIX artifacts removed."

    Write-Log
    Write-Log "===== vendor local ai-dev-core ====="

    Remove-Item $vendor -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $vendor | Out-Null

    robocopy $core $vendor /E /XD .git node_modules /XF out.txt 2>&1 |
        ForEach-Object {
            $_.ToString() | Tee-Object -FilePath $out -Append
        }

    $robocopyExit = $LASTEXITCODE
    Write-Log "Robocopy ExitCode: $robocopyExit"

    # Robocopy codes 0 through 7 are successful outcomes.
    if ($robocopyExit -gt 7) {
        throw "Vendoring ai-dev-core failed with Robocopy exit code $robocopyExit."
    }

    Push-Location $extension

    try {
        Run-Native "install extension dependencies" {
            & $npm.Source ci
        }

        Run-Native "compile extension" {
            & $npm.Source run compile
        }

        $version = & $node.Source -p "require('./package.json').version"

        if ($LASTEXITCODE -ne 0 -or -not $version) {
            throw "Could not read the extension version."
        }

        $version = $version.Trim()
        $vsix = Join-Path $artifacts "ai-dev-vscode-$version.vsix"

        Run-Native "package VSIX" {
            & $npx.Source --yes @vscode/vsce package --out $vsix
        }
    }
    finally {
        Pop-Location
    }

    Require-Path $vsix "Packaged VSIX"

    $artifact = Get-Item $vsix

    Write-Log
    Write-Log "===== built artifact ====="
    Write-Log "Path: $($artifact.FullName)"
    Write-Log "Size: $($artifact.Length) bytes"
    Write-Log "Modified: $($artifact.LastWriteTime)"

    Run-Native "install VSIX into VS Code" {
        & $code.Source --install-extension $artifact.FullName --force
    }

    Write-Log
    Write-Log "===== installed extension ====="

    $installed = & $code.Source --list-extensions --show-versions 2>&1 |
        Where-Object { $_ -match "ai-dev" }

    if ($installed) {
        $installed | ForEach-Object { Write-Log $_ }
    }
    else {
        Write-Log "WARNING: VS Code did not report an extension matching 'ai-dev'."
    }

    Write-Log
    Write-Log "===== build and installation complete ====="
    Write-Log "Installed: $($artifact.Name)"

    Write-Host
    Write-Host "AI Dev plugin built from local source and installed successfully."
    Write-Host "Log: $out"
}
catch {
    Write-Log
    Write-Log "===== build or installation failed ====="
    Write-Log $_.Exception.Message
    Write-Log $_.ScriptStackTrace

    Write-Host
    Write-Host "AI Dev plugin build or installation failed."
    Write-Host "Log: $out"
    Write-Host
    Read-Host "Press Enter to close"
}


