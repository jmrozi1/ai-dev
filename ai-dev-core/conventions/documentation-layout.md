# Documentation Layout Convention

AI Dev documentation is a routing layer.

It helps AI decide which source files to read. It does not replace source code, generated config, scripts, jobs, manifests, or runtime behavior.

Source files remain final authority for exact behavior.

## Routing Hierarchy

Use this hierarchy:

```text
architecture-summary.md
  -> directory summary.md files
  -> source files
  -> source verification for exact answers
```

Architecture routes to directories.

Directory summaries route to files.

Source decides exact behavior.

Review catches routing drift.

## Primary Artifacts

Use these generated artifacts:

```text
ai-docs/
  architecture-summary.md
  summary.md
  dependency-map.md        optional
  <source-dir>/
    summary.md
```

### `architecture-summary.md`

`architecture-summary.md` is the top-level directory routing map.

It describes included source directories and points AI to the `summary.md` files that should be read next.

It should be a compact routing table, not a prose architecture essay.

It must not list source files or identify key files.

Its directory set should come from configured source discovery, not a raw filesystem walk.

It should answer:

```text
Which directory summary should AI read next?
```

The architecture summary is stored at:

```text
ai-docs/architecture-summary.md
```

## Source Directory Discovery

Use configured source discovery to decide which directories belong in generated routing docs.

The source directory set is derived from source files that remain after applying `.ai-dev.yaml` and `source.exclude`.

Do not include directories merely because they exist on disk.

Do not include excluded directories, generated docs, editor folders, dependency folders, or vendor folders unless configured source discovery includes them.

### `summary.md`

A `summary.md` file describes source files in a directory.

Each entry should help AI route from a user question to likely source files.

Example:

```markdown
# configs/

- `configs/retributionConfig.lua` — Defines `SR.specConfigs["Retribution"]`, the Retribution Paladin spec config used by the addon; read this file for Ret spell layout, row/group placement, display conditions, and defensive buff-gating rules.
```

Root source files are summarized in:

```text
ai-docs/summary.md
```

Nested source files are summarized in the matching directory summary:

```text
configs/retributionConfig.lua
  -> ai-docs/configs/summary.md
```

### `dependency-map.md`

A dependency map is optional.

Use it when important project relationships are implicit rather than handled by normal IDE dependency tracking.

Examples:

- manifest or `.toc` load order
- global namespace creation
- shared registries
- producer/consumer relationships
- config registry population
- event or lifecycle wiring
- plugin or extension-point wiring

Do not put large reverse-dependency lists into ordinary summary entries.

## Summary Entry Rules

Summary entries should be compact and proportional to source complexity.

Prefer this shape:

```text
<source path> — Defines/owns <stable thing>; read this file for <routing reasons>.
```

Do not duplicate volatile source values such as current thresholds, row coordinates, durations, table entries, spell lists, or branch details unless they are essential routing anchors.

## Staleness Rules

An architecture summary is stale when it routes AI to the wrong directory, omits a directory whose summary should be part of top-level routing, or describes the wrong directory role.

A directory summary is stale when it routes AI to the wrong source file or describes the wrong file role.

A summary is not stale merely because exact source values changed.

Examples that usually do not require summary updates:

- cooldown changed
- threshold changed
- row coordinate changed
- table entry changed
- implementation branch changed

Those require source verification when answering questions, not summary rewrites.

## Generated Documentation Doctrine

No metadata until a tool consumes it.

No section until a workflow consumes it.

No prose unless it improves routing.
