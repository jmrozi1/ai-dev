# Review Documentation

## Purpose

Review changed source files and determine whether generated AI routing artifacts are still accurate and useful.

This workflow does not perform a full code review.

It focuses on routing correctness:

* Does `summary.md` still help AI route to the right source files?
* Did a source file’s role change enough that its summary entry should change?
* Did a source move, rename, addition, or deletion require summary updates?
* Did implicit dependency structure change in a way that requires `dependency-map.md` updates?
* Are generated routing artifacts missing, stale, misleading, or too verbose?

Source files remain final authority.

Generated summaries and dependency maps are routing aids, not source mirrors.

## Inputs

The workflow should be given:

* repository root
* changed source files
* git diff for changed files, when available
* relevant `summary.md` files, when available
* relevant `dependency-map.md`, when available
* relevant README files, when useful
* deterministic mapping or routing findings, if available
* project profile, if available
* review finding template

The default review scope is changed files.

## Core Rules

* Treat source files as final authority.
* Treat `summary.md` as a generated routing map.
* Treat `dependency-map.md` as a generated map of implicit project relationships.
* Do not require generated docs to mirror source implementation.
* Do not require summary updates for ordinary source value changes.
* Do not warn about missing frontmatter, hashes, timestamps, or reviewed flags.
* Do not silently rewrite documentation.
* Produce review findings with evidence and suggested action.
* Mark uncertainty clearly.
* Prefer precise findings over broad commentary.
* Distinguish routing problems from source behavior changes.
* Do not invent relationships.
* Do not recommend documentation updates just because exact implementation values changed, unless generated routing text duplicated those values or became misleading.

## Review Checks

### 1. Summary coverage

For each changed source file, check whether the appropriate scope `summary.md` routes to that file when it should.

Create a finding when:

* a new source file lacks a useful summary entry
* a deleted source file is still listed in a summary
* a renamed or moved source file has stale summary routing
* a source file’s role changed but the summary still describes the old role
* a summary entry routes questions to the wrong file
* a summary entry is too vague to help routing

Do not create a finding when:

* the source changed only exact values, table entries, ordering, thresholds, durations, coordinates, spell names, or implementation details
* the summary still correctly says what the file is for and when to read it

### 2. Summary quality

A useful summary entry should be compact, purpose-first, and routing-focused.

An ordinary entry should usually answer:

```text
<source path> — <stable purpose or responsibility>; read this file for <routing reasons>.
```

A complex orchestration or behavioral root may use a short structured block when a one-line entry would discard stable lifecycle, outputs, side effects, responsibility boundaries, or behavior established through supplied dependencies.

Flag summary entries that:

* are missing
* are misleading
* are bloated with source paraphrase
* duplicate volatile values
* fail to identify the file’s stable purpose, role, or observable outcome
* list implementation files while omitting the behavior they collectively implement
* omit an important stopping boundary or excluded outcome that materially distinguishes the artifact's responsibility
* replace behavior already established by dependency context with “read another file”
* fail to include important stable project symbols when those symbols are needed for routing

Do not require polished prose.

Do not require long explanations.

### 3. Dependency-map impact

Use this check when the project has dependency tracking enabled or when `dependency-map.md` exists.

Review whether the changed source affects implicit project relationships such as:

* load order
* `.toc` or manifest-driven inclusion
* global namespace creation
* shared registry population
* producer/consumer relationships
* config registries
* event or lifecycle wiring
* plugin or extension points

Create a finding when:

* a changed file now defines or consumes an important project symbol not reflected in `dependency-map.md`
* load order changed but the dependency map still shows the old order
* a producer/consumer relationship changed
* a deleted or moved file remains in the dependency map
* a new config registry entry or global routing hub was added and should be reflected

Do not create a finding for generic utility usage unless it affects project-level routing.

### 4. Source move, delete, and addition handling

For source moves or renames:

* check whether summaries still reference the old path
* check whether the new path is represented
* check whether dependency-map references need to move

For source deletions:

* check whether summaries or dependency maps still route to the deleted file

For new source files:

* check whether they need a summary entry
* check whether they affect dependency-map relationships

### 5. Non-semantic changes

If the diff is comment-only, formatting-only, or otherwise non-semantic, report no summary update needed unless the comment changes routing-relevant meaning.

A valid finding is:

```text
No routing documentation changes required.
```

Use severity `info`.

### 6. Exact value changes

Exact value changes usually do not require summary updates.

Examples that normally do not require summary changes:

* cooldown threshold changed
* spell list changed
* coordinate changed
* duration changed
* row order changed
* table entry added or removed

These require source verification when answering questions, but they do not require routing summary updates unless the summary duplicated the exact value or claimed something now false.

### 7. README impact

README review is optional.

Only recommend README updates when the source change alters human-facing setup, scope purpose, major usage, or important project behavior.

Do not require README updates for routine source changes.

## Finding Severity

Use these severity levels.

### blocking

Use when generated routing will likely send AI or humans to the wrong place.

Examples:

* summary routes to deleted or renamed source
* dependency map shows incorrect load order for a critical path
* summary claims a file owns behavior that moved elsewhere

### warning

Use when routing is incomplete, stale, or likely to cause extra investigation.

Examples:

* new source file lacks summary entry
* dependency-map missing a project-level producer/consumer relationship
* summary is too vague to route well
* summary duplicates volatile values and is likely to drift

### info

Use when no action is needed or when the finding is advisory.

Examples:

* comment-only change does not require summary update
* exact value change does not require summary update because the summary remains correct
* dependency-map unaffected

## Finding Format

Use the finding template when available.

Each finding should include:

* title
* severity
* category
* affected source file
* affected summary or dependency-map file
* evidence
* impact
* suggested action
* whether an AI-generated update is appropriate
* uncertainty, if any

## Example Findings

### Missing summary entry

```markdown
## Finding: Missing routing summary

**Severity:** warning

**Category:** Missing summary

**Source file:**
`configs/holyConfig.lua`

**Documentation file:**
`ai-docs/configs/summary.md`

### Evidence

The source file exists and defines a new spec config, but the scope summary does not list it.

### Impact

AI may not route Holy Paladin layout questions to the new config file.

### Suggested action

Add a compact summary entry for `configs/holyConfig.lua`.

### AI-generated update appropriate?

Yes. AI can inspect the source file and propose a routing-focused summary entry.

### Uncertainty

None.
```

### Stale summary path

```markdown
## Finding: Summary routes to deleted source file

**Severity:** blocking

**Category:** Stale summary

**Source file:**
`configs/retributionConfig.lua` moved to `configs/paladin/retributionConfig.lua`

**Documentation file:**
`ai-docs/configs/summary.md`

### Evidence

The summary still lists `configs/retributionConfig.lua`, but that path no longer exists.

### Impact

AI will route users to a missing file.

### Suggested action

Update the summary entry to the new source path.

### AI-generated update appropriate?

Yes. This is a mechanical routing update.

### Uncertainty

None.
```

### Exact value change, no summary update needed

```markdown
## Finding: No routing documentation changes required

**Severity:** info

**Category:** No documentation change needed

**Source file:**
`configs/retributionConfig.lua`

**Documentation file:**
`ai-docs/configs/summary.md`

### Evidence

The diff changes a cooldown value, but the summary only says this file defines the Ret spec config and should be read for Ret layout and display conditions.

### Impact

The summary remains accurate because exact cooldown values live in source.

### Suggested action

No summary update required.

### AI-generated update appropriate?

No.

### Uncertainty

None.
```

### Dependency-map update needed

```markdown
## Finding: Dependency map missing new registry consumer

**Severity:** warning

**Category:** Dependency map update needed

**Source file:**
`frames/renderSpec.lua`

**Documentation file:**
`ai-docs/dependency-map.md`

### Evidence

The changed file now reads `SR.specConfigs` to render active spec rows, but `dependency-map.md` does not list it as a consumer.

### Impact

AI may miss the connection between spec config files and the rendering pipeline.

### Suggested action

Update `dependency-map.md` to list `frames/renderSpec.lua` as a consumer of `SR.specConfigs`.

### AI-generated update appropriate?

Yes. AI can update the dependency map using the source reference as evidence.

### Uncertainty

None.
```

## Final Response

When completing this workflow, summarize:

* number of changed source files reviewed
* summary updates required
* dependency-map updates required
* no-action findings
* high-priority routing problems
* suggested next action
