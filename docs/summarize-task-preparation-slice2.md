# Summarize Task Preparation Slice 2 (Issue #15)

## Scope

Slice 2 adds deterministic summarize execution preparation on top of Slice 1 planning.

Implemented in this slice:

- repository-owned summarize batch configuration
- deterministic batch partitioning from summarize plans
- immutable summarize batch task generation
- immutable coordinator task generation
- deterministic summarize manifest generation
- atomic current-task pointer update to coordinator task
- compact invocation rendering and delivery via Issue #14 adapters

Not implemented in this slice:

- post-execution verification of generated summaries
- summarize model ownership or chat UI ownership
- review migration

## Batch Configuration

Repository config section:

```yaml
summarize:
  batch:
    max_files: 20
  rules:
    - match: "**/*"
      instructions: |
        Summarize purpose and externally visible behavior.
```

Behavior:

- `summarize.batch` is optional
- `summarize.batch.max_files` is optional
- default `max_files` is `20`
- `max_files` must be an integer greater than zero
- `bool` values are rejected (`true` and `false` are invalid)
- unknown keys under `summarize.batch` are rejected
- errors include repository file path and exact field path
- user machine config does not override summarize batch behavior

## Deterministic Batching

Batches are generated from ordered summarize plan entries.

Rules:

- source lexical order is preserved
- partitioning is by fixed `max_files` only
- no source is duplicated
- no source is omitted
- same inputs produce identical batches and IDs
- changing only batch size changes boundaries deterministically

Per-batch metadata:

- parent plan ID
- one-based batch index
- total batch count
- ordered source entries
- deterministic batch ID
- expected output paths
- source count

Task ID convention for batch tasks:

- `summarize-<plan-id>-batch-001`

Coordinator task ID convention:

- `summarize-<plan-id>-coordinator`

## Task and Manifest Design

### Coordinator task (preferred multi-batch design)

A single coordinator task is generated and set as the current task pointer target.

Coordinator contains:

- plan ID
- requested glob
- ordered batch task paths
- manifest path
- execution instruction to run all batches in order

### Batch task structure

Each batch task is provider-neutral Markdown with:

- deterministic metadata
- explicit execution rules (no source modification, write only expected outputs)
- per-source manifest:
  - source path
  - output path
  - ordered applicable instructions
  - matched rule indexes
  - required output marker and summary structure
- expected output manifest section
- completion report requirements

Expected output manifest JSON is emitted from structured data via JSON serialization
(`json.dumps(..., indent=2, ensure_ascii=False, sort_keys=True)`), so it remains
mechanically parseable for legal repository-relative filenames, including quotes,
Unicode, and backslash characters.

Required summary marker format:

```markdown
# Summary

Source: <repository-relative-source-path>
Generated-By: ai-dev summarize
Plan-ID: <plan-id>
```

### Summarize manifest

Slice 2 writes a derived deterministic manifest:

- `.ai-dev/summarize/<plan-id>/manifest.json`

Manifest includes:

- plan ID
- requested glob
- coordinator task path
- ordered batch task paths
- batch-level source/output pairs

Immutability behavior:

- coordinator task path is immutable per deterministic plan
- batch task paths are immutable per deterministic plan
- summarize manifest path is immutable per deterministic plan

## Artifact Atomicity and Rollback

Preparation behavior is all-or-nothing:

1. validate immutable coordinator/batch task path collisions up front
2. validate immutable summarize manifest collision up front
3. write coordinator and batch tasks atomically
4. write summarize manifest atomically
5. update `.ai-dev/current-task.md` atomically

If any write fails, newly created summarize artifacts are rolled back.

## CLI Behavior

`flow summarize <glob>` now means task preparation succeeded, not summary execution completed.

Execution flow:

1. resolve repository root
2. load summarize config
3. discover sources and build deterministic plan
4. partition deterministic batches
5. deterministically plan coordinator metadata (ID/path) without writes
6. load machine task config
7. validate and render compact invocation from `ai.invocation`
8. construct delivery adapter for `ai.delivery`
9. create coordinator and batch task artifacts
10. write summarize manifest
11. update current-task pointer to coordinator task
12. deliver invocation through configured adapter

Malformed invocation or delivery configuration errors are raised before persistent
summarize artifacts are created; existing task pointer and summarize artifacts are
left unchanged on these failures.

Preferred compact invocation remains:

- `Read and execute .ai-dev/tasks/<coordinator-task-id>.md`

## Slice 3 Boundary

Post-execution verification remains Slice 3 work:

- expected output existence checks
- marker and structure checks
- freshness checks
- deterministic completion/failure accounting
- source-preservation enforcement after execution
