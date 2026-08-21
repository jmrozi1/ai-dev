# flow-report.ps1 delegates to the canonical Flow helper entrypoint.
& (Join-Path $PSScriptRoot 'invoke-flow.ps1') "report" @args
