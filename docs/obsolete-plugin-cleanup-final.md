# Obsolete Plugin Cleanup Final

## Scope

Issue #18 checkpoint 3 removes obsolete extension packaging/distribution machinery and resolves the retained-extension contradiction by removing the VS Code extension package completely.

Preferred outcome selected: remove the extension package completely.

## Inventory And Classification

| Item | Classification | Decision |
| --- | --- | --- |
| `ai-dev-vscode/` package | remove now | Deleted entire extension package and lockfile. |
| `scripts/build-vscode-plugin.cjs` | remove now | Deleted. |
| `scripts/build-vsix.sh` | remove now | Deleted. |
| `scripts/update-vscode-plugin.ps1` | remove now | Deleted. |
| `ai-docs/ai-dev-vscode/` summaries | remove now | Deleted extension-specific summaries. |
| Root extension install/distribution documentation | remove now | Removed extension availability/install claims from supported path docs. |
| `docs/obsolete-plugin-cleanup-final.md` prior extension-retained framing | remove now | Rewritten for coherent CLI-only outcome. |
| Portable CLI/core workflows (`ai_dev_flow/`, `ai-dev-core/`) | retain | Kept as canonical runtime surface. |
| Provider convention wording | move to portable CLI/core | Updated to avoid claiming unsupported direct execution behavior. |
| Historical checkpoint docs (`docs/obsolete-plugin-cleanup-checkpoint1.md`, `docs/obsolete-plugin-cleanup-checkpoint2.md`) | defer with explicit justification | Retained as historical records only. |

## Coherent Architecture Decision

- No supported VS Code extension remains.
- `ai-dev config` is the supported configuration editing path.
- Workflow execution is canonical CLI-only.

## Before/After Comparison

| Metric | Pre-Issue-18 Baseline | Final State |
| --- | ---: | ---: |
| Production source files (`ai-dev-vscode/src`, excluding `src/test`) | 35 | 0 |
| Production source LOC (`ai-dev-vscode/src`, excluding `src/test`) | 17,788 | 0 |
| Extension source files (`ai-dev-vscode/src`, excluding `src/test`) | 35 | 0 |
| Extension LOC (`ai-dev-vscode/src`, excluding `src/test`) | 17,788 | 0 |
| Direct runtime dependencies (`ai-dev-vscode/package.json` `dependencies`) | 0 | 0 |
| Test files (`ai-dev-vscode/src/test/*.ts`) | 1 | 0 |
| Test LOC (`ai-dev-vscode/src/test/*.ts`) | 7,124 | 0 |
| VS Code contributed commands | 2 | 0 |
| Views and view containers | 1 view / 1 container | 0 / 0 |
| Activation events | 2 | 0 |
| Build scripts for extension distribution | 2 | 0 |
| Install/update scripts for extension distribution | 1 | 0 |
| Vendored files / LOC under extension | 25 / 2,795 | 0 / 0 |
| Supported command surface (extension commands) | `aiDev.launchAssistant`, `aiDev.settings` | none |
| Installation steps for supported extension path | 2 | 0 |
| Platform-specific extension packaging code | Present | Removed |

## Removed Architecture

Major removed subsystems across Issue #18 final state:

- direct model execution;
- chat/session state;
- terminal UI;
- slash commands;
- terminal synchronization/rendering;
- launcher view;
- obsolete settings/help behavior;
- extension packaging/vendor/install/update machinery.

## Retained Architecture

Retained supported integration:

- portable CLI/core workflows;
- editable configuration through `ai-dev config` and `ai-dev config apply`.

## Dependency And Lockfile Cleanup

- Removed extension package and lockfile instead of regenerating retained dependency graphs.
- No unrelated retained dependency versions were changed.

## Repository-Wide Stale Reference Cleanup

Removed active references to:

- extension package installation/distribution paths;
- extension summary inventory artifacts;
- extension availability in supported-path docs.

Remaining mentions of removed extension internals are historical-only in checkpoint documentation.

## Validation

Validation executed for this checkpoint correction:

- Full Python suite: `python -m unittest discover -s tests` passed (401 tests, 4 skipped).
- Targeted bootstrap/alias/config/summarize/review suites passed: `python -m unittest tests.test_bootstrap tests.test_bootstrap_cli tests.test_alias_config tests.test_alias_installation tests.test_config_open_cli tests.test_config_apply_cli tests.test_summarize_cli_preparation tests.test_summarize_planning tests.test_summarize_task_generation tests.test_summarize_verify_cli tests.test_review_cli_preparation tests.test_review_task_generation tests.test_review_verify_cli tests.test_flow_review`.
- Shell suites:
	- `tests/shell/bootstrap/test-bootstrap-linux.sh` passed.
	- `tests/shell/flow/test-flow-config.sh` passed.
	- `tests/shell/flow/test-flow-help.sh` passed.
	- `tests/shell/flow/test-flow-review.sh` passed.
	- `tests/shell/flow/test-flow-lifecycle.sh` passed.
	- `tests/shell/flow/test-flow-start.sh` remains environment-sensitive and failed with known `.ai-dev/config.json` visibility expectation mismatch.
- Repository stale-reference searches were run for removed extension/VSIX surfaces; remaining hits are historical checkpoint docs and explicit cleanup assertions.
- Clean CLI smoke with no extension installed passed: `python -m ai_dev_flow.cli --help`, `python -m ai_dev_flow.cli config --help`, `python -m ai_dev_flow.cli summarize --help`, and `python -m ai_dev_flow.cli review --help`.
- Extension compile/test/package smoke was intentionally not run because the extension package was removed.

Workflow coverage statement:

- `ai-dev summarize ...` is validated by summarize planning/task/verification tests.
- `ai-dev summarize-verify ...` is validated by summarize verification tests.
- `ai-dev flow review` is validated by review tests.
- `ai-dev review-verify ...` is validated by review verification tests.
- `ai-dev config` and `ai-dev config apply` are validated by config and bootstrap/alias tests.
- Canonical Markdown task/report consumption works without any VS Code extension installed.
