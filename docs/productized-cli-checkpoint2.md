# AI Dev Issue #19 - Checkpoint 2

## Scope

Checkpoint 2 productizes managed installation reconciliation behind `ai-dev apply` and wires it into the registry-driven CLI model.

Implemented in this checkpoint:

- Top-level canonical command: `ai-dev apply`
- Temporary compatibility route retained: `ai-dev config apply` delegates to the same apply implementation
- Managed launcher reconciliation (create/update/remove/no-op)
- Ownership manifest tracking for managed resources
- Linux managed PATH block reconciliation in `~/.bashrc`
- Rollback behavior when reconciliation fails mid-apply

## Non-Goals

Intentionally excluded from checkpoint 2:

- `ai-dev update` implementation changes
- Removal of temporary top-level lifecycle compatibility routes introduced in checkpoint 1
- Full Windows shell PATH integration (PATH block management remains Linux-only in checkpoint 2)

## Final User Config Schema (Current)

`ai-dev apply` reads user config from `AI_DEV_CONFIG` override or platform default path and consumes:

```yaml
installation:
  aliases:
    enabled: true
    expand_subcommands: true
    commands:
      flow: "ai-dev flow"
  shellPath:
    enabled: true
```

Validation highlights:

- `installation`, `installation.aliases`, and `installation.shellPath` must be mappings
- only `enabled`, `expand_subcommands`, and `commands` are allowed under `installation.aliases`
- `enabled` must be boolean when present
- `expand_subcommands` must be boolean when present
- alias names must match `^[A-Za-z_][A-Za-z0-9_-]*$`
- reserved alias names are rejected: `ai-dev`, `aidev`, `ai_dev`
- alias command values may be either non-empty command strings or non-empty argv arrays of non-empty strings
- both forms normalize to argv internally
- duplicate alias names after case-folding are rejected on case-insensitive platforms

Implemented expansion behavior:

- expansion runs only when `installation.aliases.enabled` and `installation.aliases.expand_subcommands` are both true
- expansion uses authoritative internal command-model metadata; no help-output scraping
- checkpoint 2 authoritative target: `ai-dev flow`
- `flow-help` maps to `ai-dev flow --help`
- generated lifecycle descendants map to `ai-dev flow <subcommand>` for each direct flow lifecycle command
- explicit aliases under `commands` override colliding generated descendants
- colliding generated descendants are omitted and reported in apply output
- unrecognized external commands still install root launchers but do not generate descendants; this is reported

## Validation Matrix (Checkpoint 3)

- Linux unit tests: expansion planning, suppression precedence, stale cleanup, ownership checks, and deterministic reconciliation behavior.
- Linux integration tests: generated POSIX launcher execution, argument forwarding, exit-status propagation, and idempotent re-apply.
- Linux shell-discovery validation: Bash command-name discovery (`compgen -c flow-`) with managed launcher directory on `PATH`; no custom completion scripts.
- Windows mocked tests: deterministic Windows `.cmd` launcher rendering, `%*` forwarding, percent/quote escaping, spaces/backslashes, and case-insensitive collision handling.
- Native Windows runtime validation: not performed in this Linux-only environment.

## Managed Resource Definition

Managed resources reconciled by `ai-dev apply`:

- Launcher files:
  - Linux/macOS-style behavior path: `~/.local/bin/<alias>`
  - Windows behavior path: `%LOCALAPPDATA%/ai-dev/bin/<alias>.cmd` (fallback under home if `LOCALAPPDATA` is not set)
- Linux PATH marker block in `~/.bashrc`:
  - Begin marker: `# >>> ai-dev managed PATH >>>`
  - Managed line: `export PATH="$HOME/.local/bin:$PATH"`
  - End marker: `# <<< ai-dev managed PATH <<<`
- Ownership manifest recording managed launchers and PATH block ownership digest

## Reconciliation Algorithm

High-level flow for `ai-dev apply`:

1. Ensure editable user config exists and load desired installation state.
2. Resolve managed paths by platform.
3. Load manifest (or default empty ownership model).
4. Build desired launcher contents from alias command mappings.
5. Preflight ownership checks:
   - refuse overwrite of unowned existing launcher
   - refuse update/remove of divergent launcher content
   - refuse malformed PATH marker topology
   - refuse replace/remove of unmanaged PATH block
6. Snapshot target files for rollback.
7. Apply file mutations:
   - create/update launchers
   - remove stale previously managed launchers
   - apply/remove managed PATH block (Linux)
8. Recompute digests and write updated manifest atomically when changed.
9. On failure, restore file snapshots and prior manifest best-effort; surface rollback failure details if rollback is incomplete.

`ai-dev apply` is idempotent for unchanged desired state.

## Manifest Location And Schema

Manifest locations:

- Linux/macOS-style behavior path: `~/.config/ai-dev/installation-manifest.json`
- Windows behavior path: `~/.ai-dev/installation-manifest.json`

Schema (version 1):

```json
{
  "version": 1,
  "managed_launchers": {
    "/absolute/path/to/launcher": "<sha256>"
  },
  "managed_path_block_file": "/absolute/path/to/.bashrc or null",
  "managed_path_block_sha256": "<sha256 of exact managed block text> or null"
}
```

## Linux Behavior And Examples

Example launcher content (POSIX):

```sh
#!/usr/bin/env sh
# AI_DEV_MANAGED_LAUNCHER_V1
set -eu
exec 'ai-dev' 'flow' 'commit' "$@"
```

Operational outcomes emitted by `ai-dev apply` include launcher counters and PATH/manifest status:

- launchers: `created`, `updated`, `removed`, `unchanged`
- path status: `added`, `updated`, `removed`, `unchanged`, or `disabled`
- manifest status: `updated` or `unchanged`

## PATH Block Behavior

Linux PATH management semantics:

- If no managed block exists and `shellPath.enabled: true`, block is added.
- If managed block exists and desired text matches, no-op.
- If managed block exists and differs:
  - replace only when ownership digest matches manifest
  - otherwise refuse as unmanaged divergence
- If `shellPath.enabled: false`:
  - remove only when ownership digest matches manifest
  - otherwise refuse as unmanaged block

## Windows Behavior And Current Limitations

Checkpoint 2 Windows behavior:

- `.cmd` launchers are rendered and managed in the resolved launcher directory.
- `%*` forwarding preserves user arguments.
- `shellPath` reconciliation is disabled on Windows in checkpoint 2.

Current limitation intentionally retained for later checkpoint:

- No Windows profile/PATH mutation support in this checkpoint.

## Safety Refusal Cases

The reconciler intentionally fails closed in these cases:

- destination exists but is not a regular file
- existing launcher is unowned by manifest
- existing managed launcher digest mismatch or missing managed marker
- stale launcher removal digest mismatch or marker mismatch
- malformed PATH marker structure (duplicate/misordered markers)
- PATH block replacement/removal attempted without ownership proof
- invalid manifest version or field types

## Files Changed

- `ai_dev_flow/managed_installation.py` (new)
- `ai_dev_flow/cli.py`
- `ai_dev_flow/task_config.py`
- `tests/test_managed_installation.py` (new)
- `tests/test_config_apply_cli.py`
- `tests/test_config_open_cli.py`
- `tests/test_editable_config.py`
- `tests/test_flow_namespace.py`
- `tests/test_task_slice1.py`
- `tests/shell/flow/test-flow.sh`
- `tests/shell/flow/test-flow-help.sh`
- `tests/shell/flow/test-flow-start.sh`
- `tests/shell/flow/test-flow-patch.sh`
- `tests/shell/flow/test-flow-status.sh`
- `tests/shell/flow/test-flow-promote.sh`
- `README.md`

## Validation Matrix And Results

Checkpoint-focused Python tests:

- `PYTHONPATH=/home/jtmrozi/src/ai-dev python -m unittest tests.test_managed_installation`
  - Result: passed, 19 tests
  - Includes unmanaged exact-match PATH block refusal coverage (no implicit ownership claim).

Full Python suite:

- `PYTHONPATH=/home/jtmrozi/src/ai-dev python -m unittest discover -s tests`
  - Result: passed, 424 tests, 4 skipped

Full shell matrix:

- `bash tests/shell/flow/test-flow-help.sh`
- `bash tests/shell/flow/test-flow.sh`
- `bash tests/shell/flow/test-flow-config.sh`
- `bash tests/shell/flow/test-flow-start.sh`
- `bash tests/shell/flow/test-flow-patch.sh`
- `bash tests/shell/flow/test-flow-status.sh`
- `bash tests/shell/flow/test-flow-review.sh`
- `bash tests/shell/flow/test-flow-commit.sh`
- `bash tests/shell/flow/test-flow-reset.sh`
- `bash tests/shell/flow/test-flow-promote.sh`
- `bash tests/shell/flow/test-flow-complete.sh`
- `bash tests/shell/flow/test-flow-lifecycle.sh`
- `bash tests/shell/flow/test-flow-state.sh`
- `bash tests/shell/flow/test-flow-block-resume.sh`
- `bash tests/shell/bootstrap/test-bootstrap-linux.sh`
  - Result: all passed

Review handoff generation:

- `ai-dev flow review`
  - Result: passed
  - Review task path: `.ai-dev/review/task.md`
  - Review package path: `.ai-dev/review/package.md`
  - Review diff path: `.ai-dev/review/changes.diff`
  - Review report path: `.ai-dev/review/report.md`

Checkpoint metadata note:

- Review packages generated before committing checkpoint-2 changes may still show `Checkpoint: 1` from workflow state.
- This is expected and does not change the implementation/document scope of checkpoint 2.

## Checkpoint Metrics

- Default managed alias launchers: 12
- New top-level canonical command entries added in this checkpoint: 1 (`apply`)
- Managed PATH marker block lines: 3
- Files changed in review package summary: 16
- Diff summary from review package: 1613 insertions, 212 deletions
- New Python test modules added: 1 (`tests/test_managed_installation.py`)
- Full shell suites in validation matrix: 15

## Checkpoint 3 Follow-Ups

- Implement `ai-dev update` behavior and migration story
- Evaluate Windows PATH/profile integration for managed launchers
- Remove temporary top-level lifecycle compatibility routes when migration window closes
- Consider richer dry-run/reporting mode for apply reconciliation outcomes

## Outcome

Checkpoint 2 objective is complete:

- `ai-dev apply` is the canonical installation reconciliation command.
- Managed launchers, ownership manifest, and Linux PATH block reconciliation are implemented with idempotent and fail-closed behavior.
- Compatibility route `ai-dev config apply` remains available by delegation.
- Validation matrix and review handoff artifacts are green.