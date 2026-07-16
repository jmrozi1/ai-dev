# Generate Source Summary Entry

## Purpose

Generate or update a compact routing summary for one source unit.

A source unit may be a file, script, module, class, configuration file, addon component, Jenkins job, workflow file, or similar project artifact.

The summary exists to help AI route from a user question to the right source file while preserving enough stable behavioral context to answer high-level questions efficiently.

The summary does not replace the source file. Source files remain the authority for exact values, table contents, function bodies, detailed control flow, load-order effects, and implementation details.

When supplied dependency context establishes behavior delegated by the primary source, summarize that observable behavior as part of the primary source entry. Do not replace known behavior with instructions to read the dependency file.

## Routing Model

Generated summaries answer two closely related questions:

```text
What stable responsibility or observable outcome does this source own?
Should AI read this source file for the current task?
```

A good summary should help AI quickly determine:

* the source unit's purpose or observable outcome
* what kinds of questions should route to it
* whether it is configuration, runtime logic, orchestration, debug/test code, wiring, framework code, or support code
* meaningful delegated behavior established by supplied dependency context
* which exact source file or implementation path to inspect for deeper verification

Do not turn summaries into exhaustive human manual pages.

Do not duplicate volatile details that will become stale.

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

The output should be one compact entry suitable for insertion into a `summary.md` file.

Ordinary files should normally remain single-line entries. Complex orchestration or behavioral roots may use a short indented block when one sentence would discard important stable purpose, lifecycle, outputs, or side effects.

## Summary Entry Format

For ordinary source units, use this format:

```markdown
- `<relative/source/path>` — <purpose or stable responsibility>; read this file for <routing reasons>.
```

For a complex source unit whose observable behavior is delegated across supplied dependencies, use a compact behavioral block:

```markdown
- `<relative/source/path>`
  - **Purpose:** <stable responsibility or observable outcome>
  - **Behavior:** <important lifecycle, gates, or delegated work>
  - **Outputs/side effects:** <meaningful products or state changes>
  - **Boundaries:** <important stopping points, excluded outcomes, or responsibilities intentionally not performed>
  - **Configuration:** <important invocation or operating constraints>
  - **Implementation:** <primary routing paths or symbols>
```

Include only the labels that add meaningful information.

Preserve important boundaries when they distinguish the source unit's responsibility, such as building without deploying, promoting without rebuilding, validating without mutating state, or preparing data without publishing it.

Put purpose, observable outcome, and meaningful boundaries first. Put implementation paths last.

Do not substitute “read another file” for behavior already established by supplied dependency context.

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
* complex file: one short paragraph or compact behavioral block
* orchestration or delegated behavioral root: enough structure to preserve stable purpose, lifecycle, outputs, and side effects
* unusually large or dense file: a compact entry identifying major ownership areas without paraphrasing implementation line by line

Do not exceed what is needed for routing and high-level behavioral understanding.

If detailed implementation remains necessary, route to the authoritative source after preserving the stable observable behavior already established.

## Core Rules

* Treat the source unit as the source of truth.
* Do not invent behavior.
* Do not add metadata unless the active workflow consumes it.
* Do not emit YAML frontmatter.
* Do not create a standalone per-file doc.
* Do not duplicate volatile exact source values unless they are essential routing or behavioral anchors.
* Do not list every config entry, spell, coordinate, duration, branch, table row, or helper function.
* Prefer stable purpose, observable outcome, ownership, role, symbols, and routing reasons over volatile values.
* If the source unit configures or populates a shared project symbol, name that symbol.
* If exact values or detailed mechanics matter, the answer workflow should verify them against source.
* Preserve stable high-level behavior established by supplied source and dependency context.
* Put implementation paths after purpose, behavior, outputs, and side effects.
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
* exhaustive inputs/outputs
* exhaustive control flow
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

For complex logic or orchestration files:

* identify the main responsibility and observable outcome
* identify major stable entry points or exported surfaces only when they help routing
* preserve important lifecycle stages, gates, outputs, side effects, and stopping boundaries without paraphrasing every branch
* state important excluded outcomes when they distinguish the artifact's responsibility
* synthesize behavior delegated through supplied dependency context
* avoid listing every helper
* do not replace established behavior with “read this file for X”
* place detailed implementation routing after the behavioral summary

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

A good summary entry helps AI route to the right source file faster than reading the whole tree and preserves the stable behavioral reason that the file matters.

The summary is successful if AI can:

* identify the source unit's purpose or observable outcome
* decide whether the source file is relevant
* understand why it may be relevant
* answer high-level behavioral questions when supplied context established the answer
* know when to inspect source for exact values or deeper mechanics

The summary is not successful if it becomes a mini manual, duplicates source, or requires frequent updates for ordinary source value changes.

## Failure Cases

Do not produce a source paraphrase.

Do not produce a standalone per-file documentation page.

Do not duplicate volatile values.

Do not create metadata the toolchain does not use.

Do not hide uncertainty by inventing relationships.

Do not generate a summary that is longer than necessary for routing and stable high-level behavioral understanding.

Do not produce an entry that names every implementation file but omits the outcome those files collectively implement.

## Final Response

Return only the complete updated raw Markdown contents of the target `summary.md` file.

Do not append an execution report, source-count recap, routing-role recap, parent-regeneration note, separator, or explanatory text.

Do not wrap the result in a Markdown code fence.
