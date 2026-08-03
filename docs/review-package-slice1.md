# Review Package Slice 1 (Issue #16)

## Scope

Slice 1 introduces deterministic, offline-capable review package preparation behind existing `flow review` and `flow review --all` output behavior.

Implemented in this slice:

- deterministic review context model with explicit committed vs overlay scope metadata
- deterministic review ID derived from stable structured payload
- deterministic artifact layout under `.ai-dev/review/`
- explicit `changes.diff` section boundaries and digest tracking
- NUL-safe Git path collection for changed-path metadata
- package markdown as metadata/navigation only (no embedded full patch)
- immutable package semantics with idempotent identical replay
- rollback behavior on partial write failure

Not implemented in this slice:

- generated AI task delivery for review packages
- review report validation
- model invocation for review execution
- summarize workflow behavior changes

## Offline Local Metadata Boundary

Runtime review package preparation does not invoke `gh` or network APIs.

Issue body / acceptance source resolution order:

- repository-local issue files by convention:
  - `issues/<issue-number>.md`
  - `docs/issues/<issue-number>.md`
  - `tickets/<issue-number>.md`

If unavailable locally:

- review still succeeds
- package diagnostics record local metadata limitations
- issue number/title/url still come from workflow state

## NUL-Safe Path Discovery

Changed-path discovery uses `git diff --name-only -z ...` and decodes with `surrogateescape`.

Guarantees:

- no trimming of repository path identity
- preservation of leading/trailing spaces and Unicode
- stable, deterministic ordering in package metadata

## Scoped Change Model

Workflow scope (`flow review --all`) preserves separate sections for:

- committed workflow diff (`main...scratch`)
- staged overlay diff (`HEAD -> index`)
- committed changed paths
- overlay changed paths
- combined unique changed paths
- per-scope digests and final `changes.diff` digest

Checkpoint scope (`flow review`) records only staged checkpoint changes; committed workflow section is not emitted.

## `changes.diff` Authority

`changes.diff` is the authoritative patch artifact and includes deterministic headers:

- `# AI Dev Review Changes`
- `# Scope: <workflow|checkpoint>`
- section headings for committed/overlay or checkpoint scopes

`package.json` stores `changes_diff_sha256` for the exact bytes written to `changes.diff`.

## Rolling Review Workspace

Review artifacts are working state, not a historical archive.

Canonical paths:

- `.ai-dev/review/task.md`
- `.ai-dev/review/package.md`
- `.ai-dev/review/package.json`
- `.ai-dev/review/changes.diff`
- `.ai-dev/review/report.md`
- `.ai-dev/review/verification.md`
- `.ai-dev/review/verification.json`

`Review-ID` remains inside package/task/report/verification data for integrity checks, but it no longer selects a directory path.

## Package Markdown Boundary

`package.md` does not embed full diff content.

It provides:

- metadata summary
- acceptance criteria status and local extract
- change package reference and digest
- scoped path counts
- instruction reference list
- diagnostics/limitations

## Package Schema

`package.json` is structured and versioned with deterministic key ordering:

- `schema_version`
- `review_id`
- `scope`
- `workflow`
- `repository`
- `ticket`
- `acceptance_criteria`
- `changes`
- `instructions`
- `artifacts`
- `diagnostics`

No duplicate full-context copy is stored under alternate fields.

## Replacement and Failure Semantics

- every successful `flow review` builds artifacts in a temporary sibling workspace and atomically replaces `.ai-dev/review/`
- replacement is all-or-nothing; no mixed old/new file sets are published
- failed generation cleans only the temporary workspace and preserves the prior valid `.ai-dev/review/`
- replacement starts without stale reviewer outputs from prior runs (`report.md`, `verification.md`, `verification.json`)

Lifecycle cleanup:

- successful `flow commit`, `flow reset`, `flow promote`, and `flow complete` remove `.ai-dev/review/`
- if those commands fail before completion, the rolling review workspace is preserved

## Checkpoint 2 Boundary

Checkpoint 1 stops at deterministic local package preparation.

Checkpoint 2 will cover generated task delivery and downstream review execution/validation.
