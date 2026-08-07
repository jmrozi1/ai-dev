# Summarize Planning Slice 1 (Issue #15)

Historical note: Runtime summarize commands were retired in Issue #23 checkpoint 5. This document is archived for historical reference only.

## Scope

Slice 1 introduces deterministic summarize planning for `flow summarize <glob>`.

Implemented in this slice:

- repository-owned summarize-rule loading from `.ai-dev.yaml`
- deterministic source discovery from repository-relative glob input
- deterministic rule matching and precedence ordering
- stable summary output-path mapping
- provider-neutral summarize plan construction
- CLI preview output for planning only

Not implemented in this slice:

- generated task artifact creation for summarize
- batching
- post-execution summary verification
- summarize model execution

## Repository-Owned Summarize Rules

Repository config section:

```yaml
summarize:
  rules:
    - match: "**/*"
      instructions: |
        Summarize the purpose and externally visible behavior.
    - match: "**/config.xml"
      instructions: |
        Describe configuration structure and precedence.
        Preserve exact property names and external references.
```

Validation rules:

- `summarize` must be a mapping
- `summarize.rules` must be a list
- each rule must include:
  - `match` non-empty string
  - `instructions` non-empty string
- `match` uses the same validated glob grammar as requested summarize globs
- unknown keys in `summarize` are rejected
- unknown keys in each rule are rejected
- error messages include repository config file path and field path
- declaration order is preserved

Supported summarize glob grammar (requested globs and rule `match`):

- literal path text
- `/` path separators
- `*` for zero or more non-separator characters
- `**` for recursive matching
- `?` for exactly one non-separator character
- character classes:
  - `[abc]`
  - `[a-z]`
  - negated class using Git-compatible `!`, for example `[!a]`

Rejected summarize glob syntax:

- brace expansion (`{a,b}` and any `{` or `}` usage)
- malformed character classes (unclosed `[`, empty `[]`, and invalid `[!]`)

Ownership boundary:

- summarize rules are repository-owned behavior
- user machine config does not own or override summarize rules
- issue #14 user/machine settings remain user-only (`ai.delivery`, `ai.invocation`, `editor.command`, `reports.presentation`)

## Deterministic Discovery and Ignore Behavior

Source discovery uses Git index/worktree enumeration with `git ls-files --cached --others --exclude-standard` and repository-relative glob pathspec matching.

Deterministic behavior:

- repository-relative normalized paths
- files only
- lexical ordering
- duplicate elimination
- no shell glob expansion
- no outside-repository paths
- exact Git NUL-delimited filenames are preserved without trimming, including leading/trailing spaces

Slice 1 excludes known generated/artifact prefixes from summarize planning output:

- `.ai-dev/`
- `ai-docs/`
- `artifacts/`

This is additive to Git ignore behavior and preserves existing project conventions around generated content.

## Rule Matching and Precedence

For each source file, all matching summarize rules are collected and ordered from general to specific.

Deterministic specificity model:

- fewer literal path components are more general
- more wildcard components are more general
- more literal and fewer wildcard components are more specific
- declaration order breaks ties

## Output Path Convention

Slice 1 expected summary output path convention:

- `.ai-dev/summaries/<source-relative-path>.md`

Examples:

- `src/app.py` -> `.ai-dev/summaries/src/app.py.md`
- `config/settings.xml` -> `.ai-dev/summaries/config/settings.xml.md`

## Planner Structure

Planning data includes:

- requested glob
- ordered source entries
- per source:
  - exact source path
  - exact expected output path
  - ordered applicable instructions
  - matched rule indexes
- deterministic plan identifier
- counts (sources, rules, matched rules)

The planner does not read source contents semantically, call models, write summaries, or modify source files.

## CLI Shape in Slice 1

`flow summarize <glob>` currently prints a deterministic planning preview:

- source count
- rule count
- deterministic plan id
- each source path -> expected output path with matched rule count
- explicit notice that task generation is not yet implemented in this slice

## Issue Boundary

Issue #15 Slice 1 is planning-only groundwork for summarize migration.
Subsequent slices are expected to add:

- summarize task generation
- batching
- deterministic post-execution verification
