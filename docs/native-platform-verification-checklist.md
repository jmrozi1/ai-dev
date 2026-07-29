# Native Windows Verification Checklist (Issue #14 Blocker)

This checklist records what is complete on the current development platform and what still requires native Windows execution before closing issue #14.

## Linux Status (Implemented and Natively Verified)

- Foundation behavior is implemented and verified on Linux through the current unittest and shell regression suites.
- Delivery, task generation, editor-opening behavior, and report presentation flows are validated in Linux CI-compatible tests.

## Windows Status (Mocked Coverage Complete, Native Verification Pending)

Windows-related mocked coverage includes:

- PowerShell clipboard command selection: `powershell` then `powershell.exe`
- clipboard payload passed via stdin
- no shell interpolation for clipboard command
- editor command parsing and fallback-chain logic

Native Windows verification still required:

- verify PowerShell clipboard copy places exact invocation text onto the real clipboard
- verify executable discovery in native environments (`powershell` and `powershell.exe`)
- verify quoting and file paths with spaces in real subprocess execution
- verify `notepad` fallback launch behavior
- verify configured Windows editor command parsing and launch behavior
- verify effective config path behavior (`AI_DEV_CONFIG`, `APPDATA` fallback)

## macOS Scope Note

macOS adapters are implemented on a best-effort basis but are not currently included in the supported native verification matrix for issue #14 completion.

## Recommended Issue #14 Block Reason

Awaiting native Windows verification of clipboard delivery, editor launch, quoting, and platform config-path behavior. All Linux-verifiable implementation, mocked Windows coverage, integration tests, and documentation are complete.
