# Review Task Preparation Slice 2 (Issue #16)

## Scope

Slice 2 migrates `flow review` and `flow review --all` to generated AI task preparation and provider-neutral delivery while preserving checkpoint-1 deterministic package behavior.

Implemented in this slice:

- immutable review task generation under `.ai-dev/tasks/`
- deterministic review task ID derived from deterministic review package ID
- provider-neutral invocation rendering using `ai.invocation`
- configured delivery via `ai.delivery`
- current-task pointer update to generated review task
- failure-ordering and rollback boundaries aligned to generated-task conventions
- concise review task/package/report path metadata in CLI output

Not implemented in this slice:

- review report existence or structure validation
- report presentation redesign
- automatic code fixes
- commit or promotion automation

## Output Migration Choice

Slice 2 uses **Option A (transitional compatibility)**.

Behavior:

- existing review diff output remains visible for `flow review` and `flow review --all`
- concise generated-task metadata is appended before diff output
- authoritative patch ownership remains `changes.diff`

This preserves existing operator visibility while migrating execution responsibility to generated review tasks.

## Task Identity and Paths

Task ID convention:

- `review-<review-id>-task`

Task path convention:

- `.ai-dev/review/task.md`

Review package relationship:

- one deterministic review package yields one deterministic review task
- identical replay resolves same review ID and task ID, with stable rolling task path
- materially different package content yields a different review ID but reuses the same rolling task path

## Package, Task, and Report Relationship

Review package artifacts are stored in a rolling workspace:

- `.ai-dev/review/package.md`
- `.ai-dev/review/package.json`
- `.ai-dev/review/changes.diff`
- `.ai-dev/review/report.md`

Generated review task references package artifacts and report destination by path; it does not embed full patch content.

## Read-Only Review Contract

Generated review tasks explicitly require external AI execution to:

- read deterministic package artifacts
- treat `changes.diff` as authoritative
- assess acceptance criteria, correctness, scope, safety, tests, docs, and compatibility
- distinguish blocking vs non-blocking findings
- cite file/diff locations when practical
- state uncertainty and missing context
- avoid invented repository facts
- write only the canonical report file path
- avoid modifying source files, package files, workflow state, Git state, or generated task files

The no-modification constraint is repeated prominently in task instructions and output contract.

## Required Categories and Report Contract

Task content requires at least:

- acceptance criteria coverage
- correctness
- scope control
- safety/security
- error handling
- determinism/idempotency
- test coverage
- documentation
- backward compatibility
- blocking findings
- non-blocking findings
- uncertainties/missing context

Report contract is deterministic Markdown with required sections:

- `# AI Dev Review Report`
- `Review-ID`
- `Generated-By`
- `Package-Path`
- `## Decision`
- `## Blocking Findings`
- `## Non-Blocking Findings`
- `## Acceptance Criteria Assessment`
- `## Test Assessment`
- `## Uncertainties and Missing Context`
- `## Summary`

## Delivery Integration

Slice 2 reuses generated-task delivery foundations:

- invocation template: `ai.invocation`
- delivery mode: `ai.delivery`
- supported modes: `stdout`, `file-only`, `clipboard`, `clipboard+stdout`

Delivery sends a compact task reference (task path/task identity), not full package/diff content.

## Current Task Pointer

After successful task preparation, `.ai-dev/current-task.md` is updated to:

- Task-ID: generated review task ID
- Task-Type: `review`
- Task-File: `.ai-dev/review/task.md`

Pointer never targets `package.md`.

## Rolling Replacement

- successful review generation builds in a temporary sibling directory and atomically replaces `.ai-dev/review/`
- the rolling workspace is working state, not a retained archive
- stale reviewer output files from prior reviews are not carried into the new workspace

## Failure Atomicity

Pre-write failures (for example malformed invocation template, invalid delivery configuration, adapter construction failure) occur before persistent review package/task/current-pointer writes.

Post-write failures clean only the temporary workspace from the current invocation, restore previous task pointer content when needed, and preserve the last valid rolling review workspace.

## Checkpoint 3 Boundary

Checkpoint 3 will add report validation and downstream review verification/presentation behavior.
