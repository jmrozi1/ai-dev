# AI Dev Issue #19 - Checkpoint 1

## Scope

Checkpoint 1 establishes a canonical lifecycle command namespace under `ai-dev flow` while preserving temporary top-level compatibility routes for existing lifecycle invocations.

Implemented lifecycle hierarchy:

- `ai-dev flow start`
- `ai-dev flow patch`
- `ai-dev flow task-prepare`
- `ai-dev flow status`
- `ai-dev flow review`
- `ai-dev flow commit`
- `ai-dev flow reset`
- `ai-dev flow promote`
- `ai-dev flow complete`
- `ai-dev flow block`
- `ai-dev flow resume`

Temporary compatibility routes preserved in checkpoint 1:

- `ai-dev start`
- `ai-dev patch`
- `ai-dev task-prepare`
- `ai-dev status`
- `ai-dev review`
- `ai-dev commit`
- `ai-dev reset`
- `ai-dev promote`
- `ai-dev complete`
- `ai-dev block`
- `ai-dev resume`

## Non-Goals

The following were intentionally not implemented in this checkpoint:

- `apply` command behavior changes
- `update` command behavior changes
- managed alias workflow changes
- PATH mutation behavior changes

## Before / After Command Model

Before:

- Top-level help and dispatch were partially hardcoded.
- Command ordering and alignment drifted between help output and implementation.
- Lifecycle and non-lifecycle commands were mixed in one top-level list without a canonical namespace.

After:

- Canonical lifecycle namespace is `ai-dev flow <command>`.
- Top-level help shows canonical commands first and lifecycle compatibility routes in a separate migration section.
- Command ordering is deterministic from metadata.
- Dispatch is metadata-driven through a single registry source.

## Canonical Registry Design

A `CommandSpec` dataclass registry now defines command metadata in one place, including:

- command name
- description
- namespace (`flow` vs `top`)
- deterministic order key
- handler key
- output policy metadata (`operational_config_policy`, `echo_routed_output`)
- compatibility exposure (`compatibility_top_level`)

Derived lists are generated from this registry:

- `TOP_LEVEL_CANONICAL_COMMANDS`
- `FLOW_LIFECYCLE_COMMANDS`
- `TOP_LEVEL_COMPATIBILITY_COMMANDS`

This ensures help rendering and dispatch decisions stay synchronized.

## Help Output Behavior

Top-level help now renders:

- canonical top-level command list (with `flow` first)
- explicit compatibility section for temporary lifecycle routes
- canonical guidance line for `ai-dev flow --help`

`flow` namespace help now renders:

- canonical lifecycle usage: `Usage: ai-dev flow <command> [options]`
- deterministic ordered lifecycle command rows
- command-specific help guidance for nested lifecycle commands

Unknown flow subcommand behavior is deterministic:

- error prefix: `ai-dev flow: unknown command: <name>`
- guidance: `Run ai-dev flow --help for usage.`

## Files Changed

- `ai_dev_flow/cli.py`
- `tests/shell/flow/test-flow.sh`
- `tests/shell/flow/test-flow-help.sh`
- `tests/test_flow_namespace.py`
- `README.md`
- `docs/productized-cli-checkpoint1.md`

## Validation Summary

Targeted checkpoint validations:

- `python -m py_compile ai_dev_flow/cli.py` passed
- `python -m unittest tests.test_flow_namespace` passed (6 tests)
- `bash tests/shell/flow/test-flow.sh` passed
- `bash tests/shell/flow/test-flow-help.sh` passed

Broader suite observations during this checkpoint run:

- `python -m unittest discover -s tests` failed with one unrelated existing failure:
  - `test_task_slice1.TaskSliceOneTests.test_default_configuration`
  - expected `report_presentation == "stdout"`, actual `"path-only"`
- `bash tests/shell/flow/test-flow-start.sh` failed at repo status expectation requiring untracked `.ai-dev/config.json` visibility in this environment
- `bash tests/shell/flow/test-flow-promote.sh` failed at an assertion expecting exact text `failed to switch to main` in a lockfile error scenario

These broader failures are outside the namespace/help scope of Issue #19 Checkpoint 1 changes.

## Checkpoint Metrics

- Canonical lifecycle commands under `flow`: 11
- Temporary top-level lifecycle compatibility routes: 11
- Canonical top-level command entries shown in help: 10 (`flow` + 9 non-lifecycle commands)
- Hardcoded top-level command list removed in favor of registry-derived rendering
- Hardcoded command set (`KNOWN_COMMANDS`) removed in favor of `CommandSpec` lookup
- New namespace-specific unit coverage file added: 1 (`tests/test_flow_namespace.py`)

## Outcome

Checkpoint 1 objective is implemented:

- Canonical lifecycle hierarchy is established under `ai-dev flow`.
- Temporary top-level lifecycle compatibility routes remain available.
- Help alignment and ordering are deterministic and consistent.
- Dispatch and help behavior are driven from a single command metadata registry.
