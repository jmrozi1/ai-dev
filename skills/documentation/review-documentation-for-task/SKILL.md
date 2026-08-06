---
name: review-documentation-for-task
description: >-
  Use when the user asks to review whether project documentation enables an
  engineer to accomplish a specific task, or to review and improve the complete
  documented path for that task. Coordinate documentation inventory, task and
  decision modeling, architecture review, reader simulation, procedure building,
  comparison, and evidence-based change proposals. This is the primary entrypoint
  for task-focused documentation review.
---

# Review Documentation For Task

Review whether an engineer can start from the documented entrypoint and reliably accomplish a specific task. Evaluate the documentation as one system rather than as isolated files or findings.

Do as much analysis as necessary, but expose only information that helps the user understand whether the documentation is good, what is wrong, what the correct procedure is, and what should change. Do not make the final report mirror the internal reasoning process.

## Specializations

Use these focused specializations as internal phases:

- `../review-documentation-for-task-simulate-reader/SKILL.md`
- `../review-documentation-for-task-build-procedure/SKILL.md`
- `../review-documentation-for-task-propose-changes/SKILL.md`

The coordinator owns sequencing, shared context, unresolved questions, comparison, and final judgment. Do not delegate away the overall task model or merely concatenate specialization outputs.

## Related execution skill

After producing a concrete dependency-ordered procedure, offer to walk through it interactively using `../follow-documentation-for-task/SKILL.md`. That workflow performs the procedure one step at a time in the real environment and records only exceptions that expose documentation or project-instruction gaps.

Do not begin the execution walkthrough unless the user chooses it.

## Core outcome

Determine:

1. what decisions, prerequisites, configuration, and dependencies govern the requested task;
2. whether the documentation exposes them before affected actions;
3. whether every required path is discoverable from the entrypoint;
4. what a reader actually encounters while following a concrete path;
5. how that journey differs from the correct dependency-ordered procedure;
6. which earliest document should have prevented each failure;
7. and what smallest coherent documentation changes would make the path reliable.

## Review phases

### 1. Establish task and scope

Identify the requested outcome, intended reader, likely entrypoint, environment, and completion criteria.

Do not assume there is one procedure. Ask the user when an unresolved choice materially changes which documentation or instructions apply and available evidence cannot resolve it responsibly.

### 2. Inventory relevant documentation

Inspect the primary `README.md`, documentation indexes, linked task documents, and files whose names or references make them plausibly relevant.

Do not recursively treat every Markdown file as equally relevant. Keep peripheral files as candidates and inspect them when links, references, missing concepts, or recovery needs make them relevant.

Track entrypoints, apparent task documents, discoverable links, potentially relevant but unlinked documents, and scope boundaries as working context. Do not emit a standalone inventory unless it materially explains a finding.

### 3. Model the task before evaluating instructions

Identify every decision that materially changes the procedure, including environment, deployment method, topology, inventory, target, optional components, security boundary, and supported platform or version.

Separate path-selection decisions, prerequisites, configuration, execution, verification, troubleshooting, and recovery while reasoning about the task.

Do not present or evaluate a single procedure until materially different paths are exposed. If a path must be selected for simulation, use an explicit user choice, documented default, strong environment evidence, or ask the user when ambiguity remains.

### 4. Evaluate documentation architecture

Before simulating commands, evaluate whether the documentation provides a coherent route:

- Does the entrypoint state what it covers and who it is for?
- Are supported paths and material decisions visible before branch-specific instructions?
- Does the entrypoint route the reader to every supported path needed for the task?
- Are prerequisites and configuration introduced before actions that depend on them?
- Are commands scoped to the selected environment and method?
- Can the reader discover the next required document without scanning unrelated files or already knowing its name?
- Are verification and recovery paths available?

A document that contains correct information but cannot be reached from the entrypoint does not make the task discoverable.

### 5. Simulate the reader

Load and apply `review-documentation-for-task-simulate-reader` for a concrete path. Use the real entrypoint and preserve what the reader knows at each step.

The simulation validates the architecture; it does not replace the architecture review.

### 6. Build the correct procedure

Load and apply `review-documentation-for-task-build-procedure` to synthesize the dependency-correct procedure from all relevant documentation.

This procedure is the comparison baseline, not necessarily the structure currently presented to readers.

### 7. Compare documented journey with required procedure

For each required decision, prerequisite, configuration item, action, and verification step, compare when it should become known with when or whether the documented path exposes it.

Classify material failures such as:

- missing or unclear scope;
- unreachable path;
- missing decision point;
- hidden prerequisite;
- configuration introduced after dependent execution;
- branch-specific command presented as generally applicable;
- incorrect ordering;
- missing verification;
- misleading local wording;
- and documentation-architecture failure.

Assign responsibility to the earliest document that should have routed, scoped, or prepared the reader. Do not limit a fix to the document where missing information was eventually discovered.

### 8. Propose coherent changes

Load and apply `review-documentation-for-task-propose-changes` using the shared task model, architecture findings, reader simulation, correct procedure, comparison, and user notes.

The proposal must repair the end-to-end reader path, not merely resolve isolated findings.

### 9. Offer real execution walkthrough

When a concrete ordered procedure is available, ask whether the user wants to go through it one step at a time using `follow-documentation-for-task`.

If accepted, use the procedure as the execution checklist and let that skill create and maintain `./tmp/documentation-execution-<task-slug>.md`. Keep the execution walkthrough separate from the review report so real-world exception notes remain high-signal evidence for later documentation updates.

## Governing principles

- Reveal decisions and dependencies before actions affected by them.
- Route readers from the entrypoint to every supported path.
- Ask when a material choice or evidence source is ambiguous; do not compensate with broad speculative investigation.
- Preserve the current documentation approach where possible, but do not preserve an organization that prevents a reliable reader path.
- Prefer the smallest coherent set of changes over many local patches.
- Keep verified facts, inferred structure, assumptions, and user guidance distinct while reasoning, but do not expose separate metadata fields unless they matter to the user.
- Never claim the revised documentation works unless the path was actually validated.
- Prefer a short, decisive report over an exhaustive audit artifact.

## Output

The report must answer the user's main question immediately. Lead with an A-F rating and a short explanation of why.

Use this default structure:

```markdown
# Documentation review

Rating: <A-F>

## Why
- <material reason for the rating>
- <material reason for the rating>

## Correct procedure
1. <step>
2. <step>

## Changes needed
- `<file>`: <smallest coherent change and why>
- `<file>`: <smallest coherent change and why>
```

Add an `## Unresolved` section only when a material ambiguity, conflict, or missing fact prevents a confident conclusion or procedure.

Do not create standalone sections for documentation inventory, task modeling, reader-simulation metadata, evidence catalogs, architecture analysis, proposed reader paths, verification plans, or implementation options unless one is necessary to explain a material conclusion. Those are reasoning inputs, not mandatory report structure.

Keep findings consolidated. Do not repeat the same defect in multiple sections using different terminology.

For ratings below A, the `Why` section must make the deficiencies immediately understandable. A reader should not need to inspect the rest of the report to answer "is the documentation good?"

Persist working reports under `./tmp/` using stable task-derived filenames and overwrite the same task report by default. Treat them as ephemeral evidence rather than canonical documentation.

After presenting the report, offer the interactive real-execution walkthrough when a concrete procedure is available. Do not bury that offer inside a large implementation-options section.

## Completion criteria

The review is complete when:

- the task and material path decisions are understood;
- the relevant documentation has been evaluated as an end-to-end reader path;
- the required procedure is ordered by dependency;
- at least one concrete reader path has been evaluated when applicable;
- material failures are assigned to the earliest responsible document;
- the rating clearly reflects the quality of the documented path;
- the report states the correct procedure and smallest coherent changes needed;
- unresolved uncertainty is explicit rather than hidden;
- and the final output is concise enough that the overall judgment is obvious at a glance.
