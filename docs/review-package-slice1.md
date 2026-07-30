# Review Package Slice 1 (Issue #16)

## Scope

Slice 1 introduces deterministic, offline-capable review package preparation behind existing `flow review` and `flow review --all` output behavior.

Implemented in this slice:

- deterministic review context model with explicit committed vs overlay scope metadata
- deterministic review ID derived from stable structured payload
- deterministic artifact layout under `.ai-dev/reviews/<review-id>/`
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

## Immutability Semantics

- identical deterministic replay for the same review ID is idempotent and succeeds without rewrites
- divergent content for an existing review ID is rejected
- partial/corrupt existing package directories are rejected

## Checkpoint 2 Boundary

Checkpoint 1 stops at deterministic local package preparation.

Checkpoint 2 will cover generated task delivery and downstream review execution/validation.
