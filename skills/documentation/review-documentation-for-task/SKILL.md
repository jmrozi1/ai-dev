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

## Specializations

Use these focused specializations as internal phases:

- `../review-documentation-for-task-simulate-reader/SKILL.md`
- `../review-documentation-for-task-build-procedure/SKILL.md`
- `../review-documentation-for-task-propose-changes/SKILL.md`

The coordinator owns sequencing, shared context, unresolved questions, comparison, and final judgment. Do not delegate away the overall task model or merely concatenate specialization outputs.

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

Record:

- entrypoints and indexes;
- apparent task documents;
- discoverable links between them;
- potentially relevant but unlinked documents;
- and obvious scope boundaries.

### 3. Model the task before evaluating instructions

Identify every decision that materially changes the procedure, including environment, deployment method, topology, inventory, target, optional components, security boundary, and supported platform or version.

Separate:

- path-selection decisions;
- prerequisites;
- configuration;
- execution;
- verification;
- troubleshooting and recovery.

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

For each required decision, prerequisite, configuration item, action, and verification step, compare:

- when it should become known;
- when or whether the documented path exposes it;
- what the reader is encouraged to do before knowing it;
- and the consequence of the mismatch.

Classify failures such as:

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

## Governing principles

- Reveal decisions and dependencies before actions affected by them.
- Route readers from the entrypoint to every supported path.
- Ask when a material choice or evidence source is ambiguous; do not compensate with broad speculative investigation.
- Preserve the current documentation approach where possible, but do not preserve an organization that prevents a reliable reader path.
- Prefer the smallest coherent set of changes over many local patches.
- Keep verified facts, inferred structure, assumptions, and user guidance distinct.
- Never claim the revised documentation works unless the path was actually validated.

## Output

Produce one integrated review containing:

1. requested task and selected path;
2. documentation inventory and entrypoints;
3. task decisions, prerequisites, and dependency model;
4. current documented reader path;
5. required dependency-ordered path;
6. reader-simulation findings;
7. architecture and comparison findings;
8. overall A-F rating with reasons for every rating below A;
9. proposed reader path;
10. file-by-file change proposal;
11. verification plan;
12. implementation options.

Persist working reports under `./tmp/` using stable task-derived filenames and overwrite the same task report by default. Treat them as ephemeral evidence rather than canonical documentation.

## Completion criteria

The review is complete when:

- the requested task and material path decisions are explicit;
- the relevant documentation and entrypoint relationships are mapped;
- the required procedure is ordered by dependency;
- at least one concrete reader path has been evaluated when applicable;
- documented and required paths have been compared;
- failures are assigned to the earliest responsible document;
- the rating reflects scope, discoverability, ordering, ambiguity, defects, and verification;
- proposed changes repair the complete path with minimal unrelated change;
- and implementation remains separate unless explicitly authorized.
