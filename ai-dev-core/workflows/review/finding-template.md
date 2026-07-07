# Review Finding Template

Use this template when producing documentation review findings.

Findings should be precise, evidence-based, and actionable. They should help a developer decide whether to update `summary.md`, update `dependency-map.md`, make no documentation change, or investigate uncertainty.

## Finding Structure

```markdown
## Finding: <short finding title>

**Severity:** <info | warning | blocking>

**Category:** <Missing summary | Stale summary | Weak summary routing | Dependency map update needed | No documentation change needed | README update needed | Uncertainty>

**Source file:**
`<relative/source/path-or-none>`

**Documentation file:**
`<relative/summary-or-dependency-map-path-or-none>`

### Evidence

- <fact from source, diff, summary, dependency map, README, or deterministic mapping>
- <optional second fact>

### Impact

<why this matters for AI routing, dependency understanding, or source navigation>

### Suggested action

<single primary next action>

### AI-generated update appropriate?

<Yes | No | Maybe>

<brief explanation>

### Uncertainty

<uncertainty, or None>
```

## Severity Guidance

### blocking

Use `blocking` when generated routing is likely to send AI or humans to the wrong place.

Examples:

* `summary.md` routes to a deleted or renamed source file
* `dependency-map.md` shows incorrect load order for a critical path
* a summary claims a file owns behavior that moved elsewhere
* a generated routing artifact contradicts current source in a way likely to cause wrong answers

### warning

Use `warning` when routing is incomplete, stale, too vague, or likely to cause extra investigation.

Examples:

* new source file lacks a summary entry
* summary entry is too vague to route well
* summary entry duplicates volatile values and is likely to drift
* dependency map is missing a project-level producer/consumer relationship
* dependency map omits a changed load-order or shared-global relationship

### info

Use `info` when no documentation action is needed or when the finding is advisory.

Examples:

* exact value change does not require summary update
* comment-only or formatting-only change does not require summary update
* dependency map appears unaffected
* README update is optional

## Categories

Use one of these categories when possible:

```text
Missing summary
Stale summary
Weak summary routing
Dependency map update needed
No documentation change needed
README update needed
Uncertainty
```

## Review Principles

Generated documentation is a routing layer, not a source mirror.

Do not create findings merely because exact source values changed.

Examples of exact value changes that usually do not require summary updates:

* spell names
* cooldowns
* thresholds
* coordinates
* durations
* row order
* table entries
* implementation details

Create a finding only when the routing artifact became missing, stale, misleading, too vague, or inconsistent with current source.

## Example: Missing Summary

```markdown
## Finding: Missing routing summary

**Severity:** warning

**Category:** Missing summary

**Source file:**
`configs/holyConfig.lua`

**Documentation file:**
`ai-docs/configs/summary.md`

### Evidence

- `configs/holyConfig.lua` exists and defines a new spec config.
- `ai-docs/configs/summary.md` does not include an entry for `configs/holyConfig.lua`.

### Impact

AI may not route Holy Paladin layout questions to the new config file.

### Suggested action

Add a compact routing summary entry for `configs/holyConfig.lua`.

### AI-generated update appropriate?

Yes.

AI can inspect the source file and propose a routing-focused summary entry.

### Uncertainty

None.
```

## Example: Stale Summary Path

```markdown
## Finding: Summary routes to deleted source file

**Severity:** blocking

**Category:** Stale summary

**Source file:**
`configs/retributionConfig.lua`

**Documentation file:**
`ai-docs/configs/summary.md`

### Evidence

- `ai-docs/configs/summary.md` still lists `configs/retributionConfig.lua`.
- The source file has moved to `configs/paladin/retributionConfig.lua`.

### Impact

AI will route users to a missing file.

### Suggested action

Update the summary entry to reference `configs/paladin/retributionConfig.lua`.

### AI-generated update appropriate?

Yes.

This is a mechanical routing update.

### Uncertainty

None.
```

## Example: Exact Value Change, No Summary Update Needed

```markdown
## Finding: No routing documentation changes required

**Severity:** info

**Category:** No documentation change needed

**Source file:**
`configs/retributionConfig.lua`

**Documentation file:**
`ai-docs/configs/summary.md`

### Evidence

- The diff changes a cooldown value.
- The summary only says this file defines the Ret spec config and should be read for Ret layout and display conditions.

### Impact

The summary remains accurate because exact cooldown values live in source.

### Suggested action

No summary update required.

### AI-generated update appropriate?

No.

There is no documentation update to generate.

### Uncertainty

None.
```

## Example: Dependency Map Update Needed

```markdown
## Finding: Dependency map missing new registry consumer

**Severity:** warning

**Category:** Dependency map update needed

**Source file:**
`frames/renderSpec.lua`

**Documentation file:**
`ai-docs/dependency-map.md`

### Evidence

- `frames/renderSpec.lua` now reads `SR.specConfigs`.
- `ai-docs/dependency-map.md` does not list `frames/renderSpec.lua` as a consumer of `SR.specConfigs`.

### Impact

AI may miss the connection between spec config files and the rendering pipeline.

### Suggested action

Update `ai-docs/dependency-map.md` to list `frames/renderSpec.lua` as a consumer of `SR.specConfigs`.

### AI-generated update appropriate?

Yes.

AI can update the dependency map using the source reference as evidence.

### Uncertainty

None.
```

## Example: Weak Summary Routing

```markdown
## Finding: Summary entry is too vague to route reliably

**Severity:** warning

**Category:** Weak summary routing

**Source file:**
`frames/renderSpec.lua`

**Documentation file:**
`ai-docs/frames/summary.md`

### Evidence

- The summary says only "handles rendering."
- The source reads `SR.specConfigs` and renders active spec rows.

### Impact

AI may not identify this file when investigating how spec config entries become visible addon frames.

### Suggested action

Update the summary entry to mention active spec row rendering and `SR.specConfigs`.

### AI-generated update appropriate?

Yes.

AI can inspect the source and produce a more useful routing summary.

### Uncertainty

None.
```
