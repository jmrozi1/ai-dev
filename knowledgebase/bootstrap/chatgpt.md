# ChatGPT Development Bootstrap

Use these instructions when working on this repository.

## Operating model

Development work is organized around a GitHub issue and a small, reviewable implementation slice.

* GitHub issues contain requirements, planning, and acceptance criteria.
* `main` contains permanent project history with meaningful commits.
* `scratch` contains disposable checkpoint commits used during implementation.
* `flow` performs deterministic Git and workflow operations.
* ChatGPT plans, scopes, reviews, and coordinates the work.
* Copilot or another coding agent performs repository-aware implementation when appropriate.

The scripts manage workflow state. They do not decide whether work is correct, approved, or ready to promote.

## Source of truth

Before starting implementation, identify:

1. The active GitHub issue.
2. The specific implementation slice currently being worked.
3. The current repository and workflow state.

Use the issue as the source of scope. Use the repository and current diff as the source of implementation truth. Do not rely on chat history when it conflicts with either.

When testing reveals that the design has changed, update the issue rather than claiming that outdated acceptance criteria were satisfied.

## Choosing ChatGPT or Copilot

Use ChatGPT directly for:

* planning and architecture
* defining implementation slices
* drafting or updating issues and acceptance criteria
* reviewing diffs
* documentation and prose
* small, deterministic edits with clearly known locations
* deciding what validation is required

Use Copilot or another repository-aware coding agent for:

* changes across existing repository files
* work requiring navigation through unfamiliar code
* coordinated multi-file implementation
* debugging that requires inspecting surrounding code
* iterative edit-and-test loops

Choose Copilot when repository awareness provides meaningful value, not merely because code is being changed.

Start a fresh Copilot conversation after promoting a completed slice. Do not carry large amounts of stale implementation context into unrelated work.

## Implementation slices

Keep each slice:

* narrowly scoped
* independently reviewable
* testable
* promotable as one meaningful commit
* free of unrelated cleanup

Do not expand a slice merely because additional ideas appear. Record follow-up work separately.

Before implementation, give the worker a bounded request describing:

* the intended behavior
* relevant constraints
* files or areas likely involved, when known
* focused validation to run
* explicit non-goals

Inspect current source before applying exact or structural edits. Do not patch from remembered snippets.

## Validation

The implementation worker should run focused tests relevant to the slice.

Broader regression tests and meaningful runtime checks should be run before completing the issue.

Treat evidence in this order:

1. The actual diff
2. Automated test results
3. Manual runtime behavior
4. The implementation worker’s description

Passing tests do not override an incorrect diff or behavior.

## Review

Use `flow review` to generate the cumulative review package.

During review, verify:

* every changed file is expected
* the changes remain within the active issue and slice
* the implementation matches the assigned behavior
* prior design decisions were not accidentally reverted
* unrelated refactoring or cleanup was not introduced
* destructive Git or workflow behavior remains safe
* tests cover the important successful and blocked paths
* any required manual validation has been performed

Request focused corrections rather than broad rewrites when the implementation is mostly correct.

Approval belongs to ChatGPT or the user, not to `flow` or the implementation worker.

## Workflow commands

Use command-specific help for exact syntax and behavior:

```text
flow --help
flow <command> --help
```

The normal implementation lifecycle is:

```text
flow start <issue-number>
# implement and test a bounded slice
flow review
# review and correct the implementation
flow commit
flow promote "<meaningful commit message>"
```

For small, self-contained local changes that do not warrant a GitHub issue, use:

```text
flow patch "<description>"
```

Use this form when disposable scratch work should be reset from `main` before starting.

When suitable work already exists on `scratch`, use:

```text
flow patch --adopt "<description>"
```

Patch workflows still follow the same operational lifecycle: review, checkpointing, promotion, and completion.

Use patch workflows instead of the previous temporary ordinary-Git workaround for one-off local changes.

`flow commit` creates a disposable checkpoint on `scratch`.

`flow promote` places the approved cumulative change on `main` as one meaningful commit and synchronizes `scratch` with it.

Use `flow reset` only when disposable scratch work should be discarded. Confirm that anything valuable is recoverable before destructive operations.

Use `flow complete` only after the issue’s required work and validation are finished.

## Output routing

The repository may configure operational command output through:

```text
flow get out
flow set out=<path>
flow unset out
```

When output is routed to a file, successful operational output still prints to the terminal and is also written to the configured file. The terminal’s final line is `Output written to <resolved-path>`, and that confirmation line is not written into the file.

Errors and important warnings still belong in the terminal. Configuration commands (`get`, `set`, and `unset`) remain terminal-only. Help is also terminal-only and does not write to `out`.

## General rules

* Prefer deterministic workflow commands over manual Git sequences.
* Preserve established user decisions unless they are explicitly revisited.
* Do not introduce a second workflow interface alongside `flow`.
* Do not mix speculative conclusions into durable documentation.
* When a significant repository finding is verified, document it with the exact related source or tool changes in a focused commit.
