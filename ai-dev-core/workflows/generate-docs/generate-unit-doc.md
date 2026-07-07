# Generate Source Summary Entry

## Purpose

Generate or update a compact routing summary for one source unit.

A source unit may be a file, script, module, class, configuration file, addon component, Jenkins job, workflow file, or similar project artifact.

The summary exists to help AI route from a user question to the right source file.

The summary does not replace the source file. Source files remain the authority for exact behavior, values, table contents, function bodies, control flow, load order effects, and implementation details.

## Routing Model

Generated summaries answer one question:

```text
Should AI read this source file for the current task?
```

A good summary should help AI quickly decide:

* what the source unit owns or defines
* what kinds of questions should route to it
* whether the source unit is configuration, runtime logic, debug/test code, wiring, framework code, or support code
* which exact source file to open next

Do not use summaries as human manual pages.

Do not duplicate source details that will become stale.

## Inputs

The workflow should be given:

* repository root
* source unit path
* source unit contents
* existing summary entry, if present
* nearest `summary.md`, if present
* nearest scope README, if present
* project profile, if available

## Output

Create or update the summary entry for the source unit.

The summary entry is intended to be stored in the nearest appropriate `summary.md`, not in a separate per-file documentation file.

The output should be a single compact entry suitable for insertion into a `summary.md` file.

## Summary Entry Format

Use this format:

```markdown
- `<relative/source/path>` — <summary paragraph>
```

The summary paragraph should usually be one sentence.

Use semicolons if needed to keep related routing ideas together.

Preferred shape:

```text
Defines/owns <thing>; read this file for <routing reason>, <routing reason>, and <routing reason>.
```

Examples:

```markdown
- `configs/retributionConfig.lua` — Defines `SR.specConfigs["Retribution"]`, the Retribution Paladin spec config used by the addon; read this file for Ret spell layout, row/group placement, display conditions, and defensive buff-gating rules.
```

```markdown
- `BuffTest.lua` — Defines debug globals `test()` and `stopTest()` for manually watching a hard-coded player buff via `UNIT_AURA`; read this file for buff watcher debugging, `C_UnitAuras.GetAuraDataByIndex`, and player aura state transition prints.
```

## Length Guidance

The summary should be proportional to source complexity, but always routing-focused.

Use these rough limits:

* tiny/simple/debug file: one short sentence
* normal file: one sentence with 2–4 routing reasons
* complex file: one short paragraph, still suitable as a single summary entry
* unusually large or dense file: one compact paragraph that identifies major ownership areas without paraphrasing implementation

Do not exceed what is needed for routing.

If the source is so complex that a single summary cannot route well, still keep the summary compact and let future specialized artifacts handle deeper analysis.

## Core Rules

* Treat the source unit as the source of truth.
* Do not invent behavior.
* Do not add metadata unless the active workflow consumes it.
* Do not emit YAML frontmatter.
* Do not create a standalone per-file doc.
* Do not duplicate exact source values unless they are essential routing anchors.
* Do not list every config entry, spell, coordinate, duration, branch, table row, or helper function.
* Prefer stable ownership, role, symbols, and routing reasons over volatile values.
* If the source unit configures or populates a shared project symbol, name that symbol.
* If exact values or exact behavior matter, the answer workflow should read the source file.
* If behavior is inferred, keep the inference modest and label it when needed.
* If a relationship cannot be confirmed from provided context, do not state it as fact.
* Keep the entry concise.
* Every word must help routing.

## What To Include

A good summary entry may include:

* the main thing the file defines, owns, configures, wires, or supports
* stable project symbols such as globals, registries, exported functions, commands, jobs, or config keys
* the file’s role: config, runtime behavior, debug/test utility, bootstrap, renderer, adapter, selector, library, workflow, etc.
* routing reasons: the kinds of questions that should lead AI to open this file
* stable source concepts that help distinguish this file from nearby files

## What Not To Include

Do not include:

* frontmatter
* generated timestamps
* source hashes
* reviewed flags
* full inputs/outputs
* full main flow
* exhaustive function/API tables
* exhaustive edge-case tables
* operational risks
* examples
* verification notes
* uncertainty sections
* exact config values that are likely to change
* prose that merely paraphrases source line-by-line

## Config/Data File Rules

For config or data-only files:

* state what the config is for
* state the stable registry, key, or object it defines when visible
* state what kinds of questions should route to it
* do not duplicate every config value
* do not list every item in the config table
* do not copy current spell lists, durations, thresholds, coordinates, or row contents

Good:

```markdown
- `configs/retributionConfig.lua` — Defines `SR.specConfigs["Retribution"]`, the Retribution Paladin spec config used by the addon; read this file for Ret spell layout, row/group placement, display conditions, and defensive buff-gating rules.
```

Bad:

```markdown
- `configs/retributionConfig.lua` — Defines Judgment cooldown 1, Divine Steed cooldown 8, Word of Glory green glow, Final Verdict red glow, and row x/y offsets...
```

## Small/Debug File Rules

For tiny, scratch, test, or debug files:

* keep the summary especially short
* identify manual/debug entry points when useful
* identify the primary API/event being tested or observed
* do not expand obvious code into long prose

Good:

```markdown
- `BuffTest.lua` — Defines debug globals `test()` and `stopTest()` for manually watching a hard-coded player buff via `UNIT_AURA`; read this file for buff watcher debugging, `C_UnitAuras.GetAuraDataByIndex`, and player aura state transition prints.
```

## Complex Logic File Rules

For complex logic files:

* identify the main responsibility of the file
* identify major stable entry points or exported surfaces only when they help routing
* avoid full control-flow summaries
* avoid listing every helper
* prefer “read this file for X” over “this file does step 1, step 2, step 3…”

## Relationship Rules

Do not try to maintain reverse dependency lists in each summary entry.

If a relationship is obvious and essential for routing, it may be mentioned compactly.

Project-level implicit dependencies, load order, global producers/consumers, and reverse dependency maps belong in a separate generated dependency map when that feature is enabled.

The summary entry may name stable symbols to make dependency mapping easier later.

Example:

```markdown
- `configs/retributionConfig.lua` — Defines `SR.specConfigs["Retribution"]`, the Retribution Paladin spec config used by the addon; read this file for Ret spell layout, row/group placement, display conditions, and defensive buff-gating rules.
```

This names `SR.specConfigs["Retribution"]` so a dependency map can later find producers and consumers.

## Summary File Integration

The generated entry should be suitable for insertion into a scope `summary.md`.

Folder-level summaries should aggregate child entries bottom-up.

When a source file changes, update only the affected summary entry unless the file’s role or routing implications changed enough to affect parent summaries.

## Quality Bar

A good summary entry helps AI route to the right source file faster than reading the whole tree.

The summary is successful if AI can:

* decide whether this source file is relevant
* understand why it may be relevant
* know to read the source for exact details

The summary is not successful if it becomes a mini manual, duplicates source, or requires frequent updates for ordinary source value changes.

## Failure Cases

Do not produce a source paraphrase.

Do not produce a standalone per-file documentation page.

Do not duplicate volatile values.

Do not create metadata the toolchain does not use.

Do not hide uncertainty by inventing relationships.

Do not generate a summary that is longer than necessary for routing.

## Final Response

When completing this workflow, summarize:

* source unit summarized
* summary entry created or updated
* routing role identified
* whether parent `summary.md` files may need regeneration
