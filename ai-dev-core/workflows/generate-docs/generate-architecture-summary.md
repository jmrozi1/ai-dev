# Generate Architecture Summary

## Purpose

Generate or update `ai-docs/architecture-summary.md`.

The architecture summary is the top-level directory routing map for a repository.

It routes AI from a user question to the most relevant source directories and their `summary.md` files.

It does not route directly to source files.

## Routing Model

Use this hierarchy:

```text
architecture-summary.md
  -> directory summary.md files
  -> source files
  -> source verification for exact answers
```

The architecture summary answers:

```text
Which directory summary should AI read next?
```

A directory `summary.md` answers:

```text
Which source file should AI read next?
```

Source files remain the authority for exact behavior, values, implementation details, load order effects, and current source state.

## Source Directory Discovery

The source directory set must come from configured source discovery, not a raw filesystem walk.

Use the same source discovery model as Batch Summary Doc Generation:

1. Read `.ai-dev.yaml`.
2. Resolve `documentation.docsDir`.
3. Discover configured source file candidates.
4. Exclude `docsDir`.
5. Apply blacklist-only `source.exclude` patterns.
6. Collapse included source files to their parent source directories.
7. Map each source directory to its expected summary file.

Directory mapping:

```text
<root source file>
  -> ai-docs/summary.md

<source-dir>/<source file>
  -> ai-docs/<source-dir>/summary.md
```

Do not include directories merely because they exist on disk.

Do not include excluded directories.

Do not include generated documentation directories.

Do not include editor, build, dependency, vendor, generated, or hidden directories unless they are included by configured source discovery.

Existing `ai-docs/**/summary.md` files that do not map to a configured source directory should be treated as possible stale/orphan documentation, not as architecture routing inputs by default.

## Inputs

The workflow should be given:

- repository root
- workspace `.ai-dev.yaml`, if present
- docs directory path
- expected `architecture-summary.md` path
- discovered source directories from configured source discovery
- expected summary file path for each source directory
- status for each expected summary file: exists, missing, or empty
- selected existing summary file contents
- existing `architecture-summary.md`, if present
- omitted directory rows, if any

Do not require source file contents for this workflow.

## Output

Return the complete updated raw markdown contents for:

```text
ai-docs/architecture-summary.md
```

Do not emit frontmatter.

Do not wrap the output in a markdown code fence.

Do not include commentary outside the file contents.

## Document Scope

The architecture summary is directory-level only.

It should describe:

- each included source directory
- which `summary.md` file to read next
- what kind of source the directory contains
- what kinds of questions should route to that directory
- which included source directories are unsummarized or incomplete, when relevant

It must not describe individual source files.

It must not identify "key files."

It must not rank files by importance.

It must not duplicate file-level summaries.

## Required Shape

Prefer a compact routing table.

Use this general shape:

```markdown
# Architecture Summary

## Summaries

| Path | Read | Contains |
|---|---|---|
| `.` | `ai-docs/summary.md` | <compact directory-level routing summary> |
| `<source-directory>/` | `ai-docs/<source-directory>/summary.md` | <compact directory-level routing summary> |

## Unsummarized

| Path | Expected Summary | Status |
|---|---|---|
| `<source-directory>/` | `ai-docs/<source-directory>/summary.md` | Missing summary. Generate this directory summary before relying on it for routing. |
```

Omit `Unsummarized` when no selected or discovered included source directories are missing or empty.

Do not create one prose section per directory by default.

Do not add a long purpose section unless it materially helps routing.

## Directory Description Rules

Each `Contains` cell should be compact and routing-focused.

Good directory descriptions say:

- what kind of source lives in the directory
- what architectural area or feature family the directory represents
- what kinds of questions should lead AI to read that directory's `summary.md`

Keep each row short enough to scan.

Do not list source files.

Do not say "key files include..."

Do not include exhaustive package contents.

Do not copy file-level entries from `summary.md`.

Do not mention individual file paths from the directory summary.

## Missing Summary Rules

If an included source directory has no summary:

- do not infer its detailed role from source files
- do not invent architectural meaning
- list it under `Unsummarized`
- include the expected summary path
- recommend generating the directory summary before relying on it for routing

Missing summaries are routing gaps, not fatal errors.

## Unsummarized Directory Collapse Rule

When unsummarized directories form a tree, collapse them to the highest unsummarized ancestor.

Do not list every descendant of an unsummarized directory.

Exception: include a summarized descendant in `Summaries` even when its parent directory is unsummarized.

Example:

```text
src/                 missing summary
src/components/      missing summary
src/components/ui/   has summary
```

Represent this as:

```markdown
## Summaries

| `src/components/ui/` | `ai-docs/src/components/ui/summary.md` | ... |

## Unsummarized

| `src/` | `ai-docs/src/summary.md` | Missing summary. Generate this directory summary before relying on it for routing. |
```

## Existing Summary Rules

Use existing directory summaries to infer directory roles.

Summarize the directory as a whole.

Do not copy file bullets from the directory summary.

Do not include file paths from the directory summary except the summary file path itself.

If a directory summary appears stale, ambiguous, or contradictory, mention uncertainty compactly.

## Staleness Rules

The architecture summary is stale when it routes AI to the wrong directory or omits an included source directory whose summary should be part of top-level routing.

The architecture summary is not stale merely because individual source file values changed.

Examples that may require architecture-summary updates:

- a new included source directory was added
- an included source directory was removed
- a directory's architectural role changed
- a directory summary was created for a previously unsummarized included source directory
- a directory summary was removed
- a directory's summary now describes a substantially different responsibility

Examples that usually do not require architecture-summary updates:

- a source file changed exact values
- a file-level summary entry was reworded without changing the directory role
- implementation details changed inside an existing directory role
- an excluded directory changed

## Core Rules

- Architecture routes to directories.
- Directory summaries route to files.
- Source files decide exact behavior.
- Review catches routing drift.
- Use configured source discovery to determine included source directories.
- Do not list source files.
- Do not identify key files.
- Do not duplicate file-level summaries.
- Do not invent roles for missing summaries.
- Keep the document compact.
- Every row must help AI choose the next summary file to read.

## Quality Bar

A good architecture summary lets AI quickly decide which directory summary files to inspect without reading the whole repository.

It is successful when AI can:

- start from a natural-language project question
- choose one or a few relevant source directories
- read those directories' `summary.md` files
- then route to source files for exact verification

It is unsuccessful when it:

- becomes a giant file index
- lists individual source files
- guesses which files are important
- duplicates directory summaries
- describes source behavior as final authority
- hides missing summaries for included source directories
- includes excluded directories as routing targets
