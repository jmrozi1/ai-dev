# Answer From AI Docs Workflow

## Purpose

Teach an AI coding agent how to answer a user question by using repository summaries as a routing layer.

This workflow is designed to be invoked by a compact prompt that provides the repository root and the user question.

Repository summaries are not final authority. They exist to help AI choose the right source files quickly.

Source files remain final authority for exact behavior, values, implementation details, load order effects, and current source state.

## Inputs

Required inputs:

* repository root
* user question

Optional inputs:

* workspace `.ai-dev.yaml`
* root `ai-docs/summary.md`
* scoped `ai-docs/**/summary.md` files
* `ai-docs/dependency-map.md`, when present
* root `README.md` or scoped `README.md`, when useful
* source files identified by summaries or dependency map

## Output

Produce a direct answer to the user question with:

* concise conclusion first
* supporting details only as needed
* file path citations for summaries, dependency maps, and source files used
* explicit uncertainty where evidence is incomplete
* explicit note when summaries or dependency maps are missing, stale, or conflicting

## Core Rules

* Use summaries for routing.
* Use dependency maps for implicit relationships such as load order, globals, producers, consumers, and project-level wiring.
* Use source files for exact answers.
* Do not read every source file by default.
* Do not treat generated summaries as final authority.
* Do not invent behavior.
* If summaries conflict with source, source wins.
* If dependency maps conflict with source or load-order files, source/load-order files win.
* If summaries are stale, missing, or insufficient, say so.
* Cite the files used by path.
* If the answer requires code-level specifics, verify against source.

## Routing Strategy

Follow this order unless the repository layout makes a step impossible.

1. Read workspace `.ai-dev.yaml` for profile, source globs, documentation layout, and dependency-map settings.
2. Read root `ai-docs/summary.md` when present.
3. Use the root summary to choose the most relevant scoped summary, dependency map, or source file.
4. If the question routes to a child scope, read that scope’s `summary.md`.
5. Continue narrowing through scoped summaries until the likely source files are identified.
6. Read only the minimal source files needed to answer the question.
7. Read `ai-docs/dependency-map.md` when the question depends on implicit dependency structure, load order, shared globals, producer/consumer relationships, or wiring.
8. Read `README.md` only as supplemental human-facing context when it helps answer the question.

Selection guidance:

* Prioritize summary entries whose file role or routing sentence matches the question.
* Prefer narrower scoped summaries over broad repository scans.
* Prefer source files over generated summaries once the likely file is identified.
* Stop expanding once confidence is sufficient for a safe answer.

## When To Use Dependency Maps

Use `ai-docs/dependency-map.md` when the question involves:

* load order
* `.toc` files or other manifest-driven ordering
* globals or shared namespace tables
* producer/consumer relationships
* config registries
* plugin systems or extension points
* event/lifecycle wiring
* questions like “why is this used?” or “what consumes this?”

Examples:

* “Why does this Ret spell show up out of combat?”
* “How does the addon choose the active spec config?”
* “What consumes `SR.specConfigs`?”
* “Which files must load before the renderer?”
* “Where is this global populated?”

If no dependency map exists, search source directly and state that no generated dependency map was available.

## Source Verification Rules

Use summaries for routing, then verify with source when precision matters.

Always verify against source when the answer depends on exact behavior, including:

* current values
* spell names
* config entries
* row order
* coordinates
* durations
* thresholds
* conditional logic
* branching
* side effects
* execution order
* load order
* event handling
* function bodies
* error handling
* security-sensitive behavior
* operational safeguards

Verification procedure:

1. Identify likely source files from summaries and dependency maps.
2. Read the minimal source needed to validate the claim.
3. If source is absent or ambiguous, state that explicitly.
4. Prefer source evidence over generated text whenever they differ.

## Failure / Fallback Behavior

If the normal route is insufficient, broaden in controlled steps.

1. If `ai-docs/summary.md` is missing, stale, ambiguous, or insufficient, search scoped `summary.md` files.
2. If summaries still do not answer routing, search likely source files directly.
3. If the question involves implicit dependencies and `dependency-map.md` is missing or insufficient, search manifests, load-order files, globals, and symbol references directly.
4. If no reliable evidence is found, provide best-effort guidance with explicit uncertainty and next verification targets.

Staleness indicators include:

* summary references renamed or deleted files
* summary routes to the wrong source file
* summary describes a role that no longer matches the source
* dependency map disagrees with manifest/load-order files
* dependency map omits visible producer/consumer relationships relevant to the question

Exact source value changes are not automatically summary staleness unless the summary duplicated or contradicted those values.

## Answer Format

Use this output structure when practical:

1. Answer: concise, direct response to the user question.
2. Evidence: short list of file paths used.
3. Verification status: whether source verification was performed and why.
4. Uncertainty: explicit gaps, stale summaries, missing dependency maps, or assumptions.

Style rules:

* Prefer concise answers.
* Add detail only when the question requires depth or risk is high.
* Separate confirmed facts from inferred conclusions.
* Never present generated summary text as guaranteed behavior when source validation is required.
* Do not mention every file read unless it matters to the answer.

## Quality Bar

A high-quality answer should be:

* Correct: grounded in source when exact behavior matters.
* Efficient: minimal reading scope required for confidence.
* Transparent: clear about what was verified and what is uncertain.
* Traceable: file paths allow quick human follow-up.
* Safe: avoids fabricated or overconfident claims.

Before finalizing, check:

* Did I use `summary.md` for routing when available?
* Did I avoid reading the whole repository by default?
* Did I use `dependency-map.md` when the question involved load order, globals, or producer/consumer relationships?
* Did I verify code-level specifics against source?
* Did I cite the paths used?
* Did I call out missing, stale, or conflicting summaries and dependency maps?
