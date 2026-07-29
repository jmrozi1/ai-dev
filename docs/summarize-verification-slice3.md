# Summarize Verification Slice 3 (Issue #15)

## Scope

Slice 3 adds deterministic post-execution verification for summarize task preparation outputs.

Implemented in this slice:

- `flow summarize-verify [<plan-id>]` command
- immutable summarize manifest parsing and integrity validation
- preparation-time source snapshot metadata in summarize manifest
- deterministic structural output verification
- source-change and staleness detection
- unexpected output detection under `.ai-dev/summaries/`
- per-batch completion accounting
- deterministic verification artifacts:
  - `.ai-dev/summarize/<plan-id>/verification.md`
  - `.ai-dev/summarize/<plan-id>/verification.json`

Not implemented in this slice:

- semantic quality scoring of summary prose
- review migration
- model call ownership or chat UI ownership

## Command

```text
flow summarize-verify [<plan-id>]
```

Behavior:

- with `<plan-id>`: verifies that exact summarize plan
- without `<plan-id>`: resolves current summarize plan from `.ai-dev/current-task.md`
- current task must be summarize coordinator (`summarize-<plan-id>-coordinator`)
- does not regenerate tasks
- does not invoke AI
- writes deterministic verification artifacts
- presents `verification.md` through `reports.presentation`

Exit semantics:

- `0`: verification complete and valid (`overall_status=complete`)
- `1`: verification ran and found incomplete/invalid/stale state
- CLI error path (`flow: ...`, status `1`): missing/corrupt manifest, invalid usage, or resolution failures

## Manifest Snapshot Model

Slice 2 manifest entries now include preparation snapshots (authoritative digest model):

- `source_path`
- `output_path`
- `source_digest_sha256`
- `source_size_bytes`
- `batch_index`
- `matched_rule_indexes`

Rules:

- digest algorithm: SHA-256
- source file is read as bytes at preparation time
- no source content is stored
- preparation fails before writes if a source file is missing/unreadable/non-regular
- plan ID remains based on deterministic planning inputs; snapshot metadata is for verification freshness

## Manifest Integrity Validation

Verification validates manifest structure before checking outputs:

- root JSON object
- required fields and expected scalar/list/object types
- manifest plan ID matches requested plan
- coordinator and batch task paths are repository-relative
- no path traversal
- expected outputs remain under `.ai-dev/summaries/`
- no duplicate `source_path`
- no duplicate `output_path`
- coherent batch index/count ordering
- coherent `source_count` and entry lengths
- entry `batch_index` matches parent batch
- `source_digest_sha256` is valid 64-char hex

## Output Structural Validation

Each expected output is checked for:

- file exists
- regular file
- UTF-8 readability
- non-empty after trimming
- first meaningful heading is `# Summary`
- exact source marker: `Source: <source-path>`
- exact generator marker: `Generated-By: ai-dev summarize`
- exact plan marker: `Plan-ID: <plan-id>`
- marker consistency (no conflicting duplicates)

Per-output statuses:

- `valid`
- `missing`
- `unreadable`
- `empty`
- `malformed-header`
- `wrong-source-marker`
- `missing-generator-marker`
- `wrong-plan-marker`
- `stale`

## Freshness and Source-Change Rules

Source state per expected source is verified against preparation snapshot:

- unchanged
- changed (digest/size mismatch)
- missing
- not regular file
- unreadable

A summary is treated as stale when source changed after preparation.

Verification fails overall when any source changed, disappeared, became non-regular, or became unreadable.

## Unexpected Output Detection

Verification scans regular files under `.ai-dev/summaries/` and reports files not expected by the current plan manifest.

Notes:

- expected outputs are never marked unexpected
- task files, manifests, and verification artifacts are outside this root and excluded naturally
- detected plan marker from unexpected files is reported when available

## Batch and Plan Status Semantics

Batch statuses:

- `complete`: all expected outputs valid and no source-state failures in batch
- `partial`: mixed valid and invalid/missing outputs
- `failed`: zero valid outputs with at least one invalid/missing
- `untouched`: no expected outputs exist yet and no malformed existing outputs

Overall plan status precedence:

1. `stale`
2. `failed`
3. `partial`
4. `complete`

## Verification Artifacts

Markdown report:

- path: `.ai-dev/summarize/<plan-id>/verification.md`
- sections:
  - Summary
  - Source state
  - Expected outputs
  - Batch status
  - Unexpected outputs
  - Recommended next action

Machine-readable report:

- path: `.ai-dev/summarize/<plan-id>/verification.json`
- schema versioned payload
- deterministic key ordering
- UTF-8 JSON
- atomic writes

## Intentional Non-goals

Verification does not judge semantic summary quality; it validates determinism, structure, and freshness/safety conditions only.
