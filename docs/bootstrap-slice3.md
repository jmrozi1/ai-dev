# Bootstrap Slice 3 (Issue #17 Checkpoint 3)

## Scope

Slice 3 adds cross-platform bootstrap support for installing the canonical `ai-dev` command without reimplementing CLI behavior in shell wrappers.

Implemented in this slice:

- shared bootstrap core in `ai_dev_flow.bootstrap`
- canonical launcher name: `ai-dev`
- thin platform wrappers:
  - `tools/compatibility/bootstrap-ai-dev.sh`
  - `tools/compatibility/bootstrap-ai-dev.ps1`
- ownership marker and collision safety for managed launcher files
- atomic launcher writes with rollback on failure
- deterministic install reporting (installed/updated/up-to-date)
- user config creation/preservation through existing editable-config logic
- PATH diagnostics without mutating user shell configuration

Not implemented in this slice:

- commit or promote automation
- shell-specific alias reconciliation behavior changes (remains in Slice 2)

## Architecture

Platform scripts only locate repository/Python context and delegate to:

```text
python -m ai_dev_flow.bootstrap
```

All installation semantics live in Python (`ai_dev_flow.bootstrap`), which preserves one source of truth and avoids drift between POSIX/PowerShell implementations.

## Launcher Installation Behavior

Target install directory:

- default: `~/.local/bin`
- override: `--install-dir`

Generated launchers:

- POSIX: `~/.local/bin/ai-dev`
- Windows: `~/.local/bin/ai-dev.ps1` and `~/.local/bin/ai-dev.cmd`

Each launcher:

- includes ownership marker `AI_DEV_LAUNCHER_V1`
- sets `FLOW_COMMAND_NAME=ai-dev`
- prepends repository root to `PYTHONPATH`
- executes `python -m ai_dev_flow.cli` with argument forwarding
- preserves downstream exit codes

## Safety and Ownership

Before writing, bootstrap validates all destination files:

- destination must be a regular file or absent
- existing file must contain the AI Dev ownership marker
- non-owned collisions fail safely with manual recovery guidance

Writes are transactional at launcher scope:

- prior file content/mode snapshots are captured
- on write/verification failure, previous state is restored best-effort
- rollback failures are surfaced in the error message

## Runtime Validation

Bootstrap validates interpreter/runtime before writing launchers:

- resolve Python from explicit `--python`, `AI_DEV_PYTHON`, or platform defaults
- explicit interpreter must resolve or bootstrap fails
- require Python 3.8+
- require importability of `ai_dev_flow.cli`

## PATH Diagnostics

Bootstrap reports whether install directory is present on PATH:

- POSIX detection uses `:` delimiter and exact normalized path matching
- Windows detection uses `;` delimiter and case-insensitive normalized matching

When missing, bootstrap prints manual guidance but does not mutate PATH.

## CLI

Bootstrap command:

```text
python -m ai_dev_flow.bootstrap --repo-root <absolute-path>
```

Common options:

- `--platform posix|windows`
- `--python <interpreter>`
- `--install-dir <path>`
- `--config-path <path>`
- `--command-name ai-dev` (default)

Exit semantics:

- `0` on successful installation/update/no-op
- non-zero on validation/collision/runtime/write failures

## Tests

New focused coverage:

- `tests/test_bootstrap.py`
  - launcher rendering and argument forwarding
  - ownership enforcement and non-owned collision failure
  - POSIX and Windows PATH detection semantics
  - idempotency and config preservation
  - rollback behavior on write failure
- `tests/test_bootstrap_cli.py`
  - argument parsing defaults
  - success output path
  - failure exit behavior
