# Generate Documentation Workflow

## Purpose

Generate compact AI routing documentation for a repository.

The primary generated artifacts are `architecture-summary.md` and directory-level `summary.md` files.

`architecture-summary.md` maps source directories to directory summaries so AI can decide which `summary.md` files to read first.

A `summary.md` file maps source files in a directory to short routing summaries so AI can decide which source files to read for a user question.

This workflow does not generate per-file documentation pages by default.

Source files remain final authority for exact behavior, values, table contents, function bodies, control flow, load order effects, and implementation details.

## Documentation Model

Use this model:

```text
source directories
  -> architecture-summary.md
  -> directory summary.md files
  -> source files
  -> optional dependency-map.md
  -> source verification for exact answers
Examples:

ApiTest.lua
  -> ai-docs/summary.md

configs/retributionConfig.lua
  -> ai-docs/configs/summary.md

configs/unholyConfig.lua
  -> ai-docs/configs/summary.md

A directory summary should contain entries like:

# configs/

- `configs/retributionConfig.lua` — Defines `SR.specConfigs["Retribution"]`; read this file for Ret spell layout, row/group placement, display conditions, and defensive buff-gating rules.
Core Rules
Treat source files as final authority.
Use summaries for routing only.
Do not duplicate volatile source values.
Do not create standalone per-file docs by default.
Do not emit metadata unless a workflow consumes it.
Do not use hashes, timestamps, or reviewed flags unless tooling consumes them.
Keep summaries compact and proportional to file complexity.
Exact source values should be read from source, not copied into summaries.
Preserve useful existing summary entries unless clearly stale.
Remove or update stale summary entries when files move, are deleted, or change role.
Generate or Update Summary Entries

For each selected source file:

Identify the source file's parent directory.
Map that parent directory to the matching summary.md.
Add or update the source file's entry in that summary.md.
Keep existing entries for unrelated files unless they are clearly stale.
Return the complete updated raw markdown contents of the target summary.md.

Directory mapping:

<root source file>
  -> ai-docs/summary.md

<dir>/<source file>
  -> ai-docs/<dir>/summary.md
Batch Generation

Batch generation may select multiple source files.

When multiple selected source files map to the same summary.md, update that summary in one grouped operation.

The generated summary should include all selected source files and preserve useful existing entries for unselected files in the same summary.

Dependency Maps

Dependency maps are optional.

Use dependency-map.md when the project has important implicit relationships that are not handled well by normal IDE dependency tracking.

Examples:

manifest or .toc load order
global namespace creation
shared registries
producer/consumer relationships
config registry population
event or lifecycle wiring
plugin or extension-point wiring

Do not place large reverse-dependency lists inside ordinary summary entries.

Quality Bar

Generated summaries are successful when AI can:

identify likely source files from a user question
understand why a file may be relevant
avoid reading the whole repository by default
know when to inspect source for exact details

Generated summaries are not successful when they:

paraphrase source line-by-line
duplicate volatile values
become larger than needed for routing
route to deleted or wrong files
imply exact behavior without source verification
Final Response

When completing this workflow, summarize:

summary files created or updated
number of source files summarized
dependency-map updates, if any
stale or removed routing entries, if any
any uncertainty
