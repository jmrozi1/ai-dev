# AI Dev Issue #19 - Checkpoint 3

## Scope

Checkpoint 3 productizes a safe, metadata-driven `ai-dev update` command that refreshes the recorded source checkout, refreshes bootstrap launchers from updated source, and reconciles managed installation state.

Implemented in this checkpoint:

- Top-level canonical command: `ai-dev update`
- Durable installation source metadata recording during bootstrap
- Strict update preflight checks before any mutation
- Git fetch + fast-forward-only update policy
- Bootstrap refresh invoked from updated source tree with recorded branch/remote preserved
- Managed installation apply invoked from updated source tree
- Partial-failure phase reporting for source/bootstrap/apply

## Non-Goals

Intentionally excluded from checkpoint 3:

- Automatic stash/reset/clean/rebase/merge/force conflict resolution
- Windows shell profile PATH reconciliation beyond existing checkpoint-2 boundaries
- Removal of temporary compatibility routes introduced during Issue #19 migration

## Installation Source Metadata Path And Schema

Metadata file path:

- Linux/macOS-style behavior path: `~/.config/ai-dev/installation-source.json`
- Windows behavior path: `%APPDATA%/ai-dev/installation-source.json` (fallback under home roaming profile path shape)

Schema (version 1):

```json
{
  "version": 1,
  "source_repository": "/absolute/path/to/ai-dev/checkout",
  "branch": "main",
  "remote": "origin"
}
```

Validation/safety rules:

- `version` must be supported (`1`)
- `source_repository` must be a non-empty absolute path
- `branch` and `remote` must be non-empty strings
- malformed JSON or invalid fields fail closed with actionable guidance

## Bootstrap Recording Design

`run_bootstrap` now records update metadata during installation:

- Resolves canonical Git top-level for the provided repository root
- Persists metadata atomically through JSON helper APIs
- Stores configured `update_branch` and `update_remote`
- Returns metadata path and update branch/remote in bootstrap result
- Prints metadata path and update config in bootstrap output for operator visibility

Behavior guarantees:

- Recording is idempotent on repeated bootstrap runs with unchanged inputs
- Symlink-invoked repository roots are canonicalized to real Git root before recording

## Update Preflight

Before mutation, `ai-dev update` validates:

- metadata file exists and is valid
- recorded source path exists and is a Git repository
- configured remote exists in repository
- configured local branch exists
- no active Git operations (merge/rebase/cherry-pick/revert/bisect), including linked worktrees by resolving the real Git directory through Git
- working tree is clean (staged, unstaged, and untracked changes all refused)

Failure mode:

- Update aborts before mutation with explicit source/metadata diagnostics.

## Git Update Strategy

Source update flow:

1. Resolve canonical source root from metadata.
2. Ensure checkout is on approved branch (checkout allowed only when clean).
3. `git fetch <remote>`.
4. Compare local branch head with `<remote>/<branch>` head.
5. If equal: report `already up to date`.
6. If local is ancestor of remote: run `git merge --ff-only <remote>/<branch>`.
7. If diverged: refuse update with explicit divergence details.

Policy:

- Fast-forward-only.
- No merge commits, no rebase, no force mutation.

## Dirty Checkout Behavior

`ai-dev update` refuses dirty source checkouts and does not auto-heal repository state.

Refusal includes:

- repository path
- short `git status` preview
- explicit statement that stash/reset/discard are not performed automatically

## Self-Refresh And Apply Handoff

After source update phase:

- Launcher refresh runs via `python -m ai_dev_flow.bootstrap` from updated source root and forwards the recorded `branch` / `remote` values back into bootstrap metadata recording.
- Apply runs via `python -m ai_dev_flow.cli apply` with `PYTHONPATH` rooted at updated source root.

This ensures post-update phases execute the newly updated code path, not stale caller state.

## Partial Failure Behavior

`ai-dev update` reports three phases:

- `Update source`
- `Launcher refresh`
- `Apply`

Return code behavior:

- Source preflight/update failure: command fails with neutral source-phase reporting and preserved detail context.
- Launcher failure: apply is not executed; command returns non-zero.
- Apply failure after source/bootstrap success: command returns non-zero with retry guidance.

## Windows Behavior And Limits

Checkpoint 3 preserves existing platform boundaries:

- Metadata default path uses Windows roaming appdata conventions.
- Bootstrap/apply handoff runs with Windows platform selection when applicable.
- No new Windows shell PATH/profile mutation behavior added in this checkpoint.

## Files Changed

- `ai_dev_flow/update_installation.py` (new)
- `ai_dev_flow/bootstrap.py`
- `ai_dev_flow/cli.py`
- `tests/test_update_installation.py` (new)
- `tests/test_update_cli.py` (new)
- `tests/test_bootstrap.py`
- `tests/test_flow_namespace.py`
- `tests/shell/flow/test-flow-help.sh`
- `tests/shell/flow/test-flow.sh`

## Validation Matrix And Results

Focused checkpoint-3 Python suites:

- `python -m unittest tests.test_update_installation tests.test_update_cli tests.test_bootstrap tests.test_bootstrap_cli tests.test_flow_namespace`
  - Result: passed (61 tests, 2 skipped)
- Focused rerun:
  - `python -m unittest tests.test_update_installation tests.test_update_cli tests.test_bootstrap tests.test_flow_namespace`
  - Result: passed (33 tests)

Broader targeted Python suites:

- `python -m unittest tests.test_update_installation tests.test_update_cli tests.test_bootstrap tests.test_bootstrap_cli tests.test_config_apply_cli tests.test_config_open_cli tests.test_managed_installation tests.test_review_cli_preparation tests.test_review_task_generation tests.test_review_package tests.test_review_manifest tests.test_review_context tests.test_review_verify_cli tests.test_review_verification tests.test_summarize_verify_cli tests.test_summarize_verification tests.test_summarize_task_generation tests.test_summarize_planning tests.test_summarize_manifest tests.test_summarize_discovery tests.test_summarize_config tests.test_summarize_cli_preparation tests.test_summarize_batching`
  - Result: passed (235 tests, 2 skipped)

Full Python suite:

- `python -m unittest discover -s tests`
  - Result: passed (454 tests, 4 skipped)

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

## Follow-Up Work

- Add optional dry-run mode for update preflight + phase plan preview.
- Consider explicit branch/remote override flags for `ai-dev update` with strict allowlist validation.
- Evaluate richer diagnostics for launcher/apply subcommand failures.
- Plan migration removal window for temporary compatibility routes.

## Outcome

Checkpoint 3 objective is complete:

- `ai-dev update` is implemented as a safe, metadata-driven, fast-forward-only update path.
- Bootstrap now records durable source metadata for future updates.
- Source refresh, launcher refresh, and apply reconciliation execute in deterministic sequence with fail-closed behavior, preserved recorded branch/remote metadata, worktree-safe Git-operation detection, and phase-level reporting.
- Python and shell validation matrices are green.