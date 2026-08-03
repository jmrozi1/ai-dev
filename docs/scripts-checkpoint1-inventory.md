# Issue #20 Checkpoint 2: Script Surface Reduction Result

This document now records the actual post-checkpoint-2 layout and migration outcomes.

## Checkpoint 3 Final Test Interface

Checkpoint 3 completes the canonical public test entrypoint design through:

- `scripts/test.sh`
- `scripts/test.ps1`

Named suites:

- `unit` (default): fast Python unit coverage
- `bootstrap`: bootstrap-focused Python tests plus shell suites under `tests/shell/bootstrap/`
- `flow`: shell lifecycle suites under `tests/shell/flow/` (auto-discovered via `test-*.sh`)
- `integration`: broader cross-component Python discovery (`python -m unittest discover -s tests -p 'test_*.py'`)
- `all`: complete Python and shell matrix (`integration` + bootstrap shell + flow shell)

Dispatcher capabilities:

- `--help`: command usage, suite semantics, forwarded-arg rules, and PowerShell shell-policy behavior
- `--list`: suite purpose plus underlying modules/directories/scripts
- `--`: forwards unittest arguments to Python-running suites (`unit`, `bootstrap`, `integration`, `all`)
- rejects ambiguous combinations and invalid suite names with usage output

PowerShell policy for shell suites:

- run shell suites via `bash` when available
- explicitly report shell suites as skipped when `bash` is unavailable

Normal workflow guidance:

- use `scripts/test.sh` or `scripts/test.ps1` as the public test interface
- direct invocation of individual files under `tests/shell/` is internal validation detail, not the primary user workflow

## Final Public scripts/ Surface

Only the following public script entry points remain:

- scripts/install.sh
- scripts/install.ps1
- scripts/test.sh
- scripts/test.ps1

## Files Moved Out Of scripts/

| Previous path | New path | Purpose |
|---|---|---|
| scripts/python_select.sh | tools/bootstrap/python_select.sh | POSIX interpreter selection helper used by public wrappers |
| scripts/PythonSelection.ps1 | tools/bootstrap/PythonSelection.ps1 | PowerShell interpreter selection helper used by public wrappers |
| scripts/test-bootstrap-linux.sh | tests/shell/bootstrap/test-bootstrap-linux.sh | Shell bootstrap compatibility test |
| scripts/test-flow*.sh | tests/shell/flow/test-flow*.sh | Shell flow behavior test suites |
| scripts/bootstrap-ai-dev.sh | tools/compatibility/bootstrap-ai-dev.sh | Deprecated compatibility wrapper |
| scripts/bootstrap-ai-dev.ps1 | tools/compatibility/bootstrap-ai-dev.ps1 | Deprecated compatibility wrapper |
| scripts/bootstrap-linux.sh | tools/bootstrap/bootstrap-linux.sh | Deprecated compatibility wrapper |

## Removed Files

Removed because no supported dependency remained in checkpoint 2:

- scripts/bootstrap.sh
- scripts/bootstrap-flow.ps1
- scripts/bootstrap-config.yaml
- scripts/flow
- scripts/flow.ps1
- tests/test_flow_status.py
- tools/bootstrap/bootstrap-config.yaml

## Flow Launcher Retirement Decision

Standalone scripts/flow and scripts/flow.ps1 were retired in this checkpoint.

Why removal is safe now:

- moved shell flow tests no longer execute scripts/flow; they execute installed ai-dev
- Windows-only tests that directly targeted scripts/flow.ps1/bootstrap-flow.ps1 were removed with the launcher retirement
- docs and references were updated away from scripts/flow paths

## Remaining Compatibility Surfaces

These remain only as explicit deprecation bridges outside scripts/:

- tools/compatibility/bootstrap-ai-dev.sh
- tools/compatibility/bootstrap-ai-dev.ps1
- tools/bootstrap/bootstrap-linux.sh

All three print deprecation guidance and delegate to scripts/install.sh or scripts/install.ps1.

## Test Path Migration Summary

Checkpoint-2 path updates for shell suites:

- tests/shell/bootstrap/test-bootstrap-linux.sh
- tests/shell/flow/test-flow.sh
- tests/shell/flow/test-flow-help.sh
- tests/shell/flow/test-flow-start.sh
- tests/shell/flow/test-flow-patch.sh
- tests/shell/flow/test-flow-status.sh
- tests/shell/flow/test-flow-review.sh
- tests/shell/flow/test-flow-commit.sh
- tests/shell/flow/test-flow-reset.sh
- tests/shell/flow/test-flow-promote.sh
- tests/shell/flow/test-flow-complete.sh
- tests/shell/flow/test-flow-lifecycle.sh
- tests/shell/flow/test-flow-state.sh
- tests/shell/flow/test-flow-block-resume.sh
