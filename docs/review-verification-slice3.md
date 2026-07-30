# Review Verification Slice 3 (Issue #16)

## Scope

Slice 3 adds deterministic post-execution verification for review workflows.

Implemented in this slice:

- new `flow review-verify [<review-id>]` command
- current review resolution from `.ai-dev/current-task.md` when `<review-id>` is omitted
- explicit review ID validation (`review-<16 lowercase hex chars>`)
- immutable review package/task/report integrity checks
- deterministic verification artifacts (`verification.md`, `verification.json`)
- canonical review report presentation through `reports.presentation` mode with warning/path fallback
- exit status contract for complete vs incomplete/invalid verification

Not implemented in this slice:

- automatic remediation or patch generation
- commit or promotion automation
- behavioral changes to `flow review`
- behavioral changes to `flow showreport`

## Command Contract

Usage:

```text
flow review-verify [<review-id>]
```

Resolution behavior:

- if `<review-id>` is provided, verify that review directly
- if omitted, resolve from `.ai-dev/current-task.md`
- pointer must be `Task-Type: review` with consistent review task ID/path

Validation failures are surfaced as user-facing CLI errors without tracebacks.

## Verification Inputs

Expected immutable artifacts under `.ai-dev/reviews/<review-id>/`:

- `package.md`
- `package.json`
- `changes.diff`
- `report.md`

Expected generated task path:

- `.ai-dev/tasks/<review-id>-task.md`

## Integrity Model

### Package Integrity

`review-verify` checks:

- `package.md`, `package.json`, and `changes.diff` exist and are readable
- `package.md` and `package.json` are valid UTF-8 independently (distinct failure reasons)
- `package.json` is valid JSON object with `schema_version: 1`
- required top-level package payload fields are present with expected structural types
- `package.json.review_id` matches the requested review ID
- `package.json.artifacts.*` paths match deterministic expected paths
- `package.json.artifacts.*` paths are validated as repository-relative and traversal-safe before comparison
- `package.json.changes.changes_diff_path` matches deterministic expected path and is traversal-safe
- `package.json.changes.changes_diff_sha256` matches SHA-256 of `changes.diff`
- `package.md` contains exactly one `Review-ID` marker and exactly one `Changes-Diff-Path` marker

### Task Integrity

`review-verify` checks generated task file presence/readability and required deterministic markers.
Each marker must appear exactly once and must match expected value:

- `# AI Dev Generated Task: <review-id>-task`
- `- Task-ID: <review-id>-task`
- `- Task-Type: review`
- `- Task-File: .ai-dev/tasks/<review-id>-task.md`
- `- Review-ID: <review-id>`
- `- Package-Markdown-Path: .ai-dev/reviews/<review-id>/package.md`
- `- Package-JSON-Path: .ai-dev/reviews/<review-id>/package.json`
- `- Changes-Diff-Path: .ai-dev/reviews/<review-id>/changes.diff`
- `- Review-Report-Path: .ai-dev/reviews/<review-id>/report.md`

### Report Contract Validation

`report.md` must be valid UTF-8 markdown and include:

- heading: `# AI Dev Review Report`
- marker: `Review-ID: <review-id>`
- marker: `Generated-By: external AI review`
- marker: `Package-Path: .ai-dev/reviews/<review-id>/package.md`
- decision line in `## Decision`: `- Status: pass | pass-with-notes | blocked`

In addition, section structure is strict:

- required H2 headings must each appear exactly once
- required headings order is fixed:
	- `## Decision`
	- `## Blocking Findings`
	- `## Non-Blocking Findings`
	- `## Acceptance Criteria Assessment`
	- `## Test Assessment`
	- `## Uncertainties and Missing Context`
	- `## Summary`
- `- Status:` must appear exactly once and only inside `## Decision`

## Verification Status Model

Overall statuses:

- `complete`: package/task/report are structurally valid, regardless of decision (`pass`, `pass-with-notes`, or `blocked`)
- `incomplete`: report is missing, unreadable, empty, or otherwise unavailable for structural validation
- `invalid`: any integrity or report contract failure

Review decision is a separate field:

- `review_decision`: `pass` | `pass-with-notes` | `blocked` | `null`
- `blocked` is an external review decision, not a verification status
- recommended action for complete + blocked: `Address blocking findings before checkpoint or commit.`

Exit code behavior:

- exit `0` for `complete` (including complete + `blocked` decision)
- exit `1` for `incomplete` or `invalid`

## Deterministic Artifacts

Each verification run writes:

- `.ai-dev/reviews/<review-id>/verification.md`
- `.ai-dev/reviews/<review-id>/verification.json`

Properties:

- deterministic content for identical inputs
- idempotent overwrite via atomic writes
- machine-readable status/reason/action in JSON

## Presentation Behavior

`review-verify` presents canonical `report.md` using `reports.presentation` after structural validation succeeds:

- `stdout`, `editor`, or `path-only`
- presentation configuration is loaded only when verification is complete and report contract status is valid
- presenter failures emit warning and canonical report-path fallback
- report presentation is attempted only when report contract status is valid

`verification.md` and `verification.json` remain supporting diagnostics, not the primary review output.

## Boundary Confirmation

This slice intentionally keeps existing commands unchanged:

- `flow review` behavior remains unchanged
- `flow showreport` behavior remains unchanged
