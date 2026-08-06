---
name: simulate-documentation-following
description: >-
  Use when the user asks you to follow, walk through, test, validate, or evaluate
  project documentation or documented instructions, especially when the path
  taken, reader experience, failures, ambiguities, or missing information should
  be recorded. Ask whether to use a read-only walkthrough or an active walkthrough
  only when the requested mode is unclear.
---

# Simulate Documentation Following

Follow documentation as an engineer encountering the project would. Preserve the journey instead of replacing it with a synthesized answer based on knowledge gathered afterward.

## Modes

This skill has two modes.

### Read-only walkthrough

Use this mode by default unless the user explicitly asks to execute, test, install, configure, deploy, or otherwise perform the documented actions.

In read-only mode:

- do not run commands;
- do not edit project files other than writing the generated walkthrough report;
- do not change environment state;
- follow the reader-visible documentation path progressively;
- identify likely failures, ambiguities, missing prerequisites, and decision points;
- produce a journey log and documentation defect report;
- state clearly that execution was not attempted.

After the report, offer an active walkthrough as a separate next action. Do not begin execution without explicit authorization.

### Active walkthrough

Use this mode only when the user explicitly requests that the documented procedure be attempted.

In active mode:

- follow the same progressive-reading rules as read-only mode;
- execute documented actions in the order a reader would encounter them;
- verify preconditions when they become relevant;
- record exact commands or changes attempted;
- compare expected and actual results;
- record failures before investigating workarounds;
- distinguish documented actions from recovery actions;
- avoid destructive or irreversible actions unless clearly authorized;
- never claim success without observed verification.

## Core behavior

1. Start from the entry point named by the user. If none is named, start from the repository's primary `README.md`.
2. Do not begin by recursively reading all documentation or globally searching for the final answer.
3. Read the current document progressively and follow relevant links, references, commands, and prerequisites in the order a reader would encounter them.
4. Before opening another document or performing an action, record what in the current document caused that next step.
5. Record decisions when they arise. Do not silently use information discovered later to make an earlier choice appear obvious.
6. Record ambiguity, missing prerequisites, circular references, stale commands, misleading wording, incorrect ordering, hidden dependencies, and undocumented assumptions at the point where they become visible.
7. When the documented path is insufficient, explicitly mark the path as broken or incomplete before performing a targeted recovery search.
8. Keep recovery searches narrow. Search for the failed command, named component, referenced file, variable, deployment method, or other specific missing concept.
9. Distinguish information found through the documented path from information found through recovery investigation.
10. Do not retroactively rewrite the journey after discovering the correct solution.
11. Do not rewrite or reorganize documentation. Produce evidence and recommendations only.
12. Do not claim that a task works unless it was actually executed and verified.

## Report file

Persist the completed report as a Markdown file.

- Use a path supplied by the user when provided.
- Otherwise write to `./tmp/documentation-walkthrough-<task-slug>.md`, relative to the target project's current working directory.
- Create `./tmp/` when it does not exist.
- Derive a concise, filesystem-safe task slug from the requested walkthrough target.
- Overwrite an existing report with the same path; do not create numbered or dated copies unless the user requests history.
- Treat the report as ephemeral working evidence, not canonical project documentation.
- Include the final report path in the response.

## Reader simulation rules

Behave as though you cannot preload the entire documentation set into working memory.

- Do not inspect unrelated documentation merely because it might become useful later.
- Do not jump directly to a likely implementation file unless the documentation directs the reader there or the documented path has already failed.
- Do not conceal false starts, failed commands, or misleading instructions from the final report.
- Do not use later discoveries to make earlier decisions look obvious.
- Use repository knowledge or broad searching only after the documented path fails, and label that transition clearly.

## What to log

For every meaningful transition or action, record:

- **Current source:** file and heading path, when available
- **Trigger:** the text, link, command, prerequisite, result, or failure that caused the next action
- **Decision:** any choice made and the information available at that moment
- **Assumption:** anything accepted without sufficient evidence
- **Action:** document opened, command run, file changed, or check performed
- **Expected result:** what the documentation implies should happen
- **Actual result:** what happened in active mode, or the predicted outcome in read-only mode
- **Status:** continued, ambiguous, blocked, failed, recovered, or verified
- **Plain-language interpretation:** a short paragraph explaining the reader's experience at that moment

The plain-language interpretation should explain what is clear, what is confusing, and why a reasonable engineer would continue, hesitate, or become blocked. Synthesize the structured fields rather than merely restating them.

Keep the log chronological.

## Handling choices

When the documentation presents or implies alternatives:

1. List the alternatives visible at that point.
2. Record which alternative was selected, if any.
3. State whether the selection was:
   - explicitly requested by the user;
   - declared as the documented default;
   - inferred from known environment context;
   - assumed to continue;
   - or unresolved.
4. If the choice materially changes the path and cannot be resolved, preserve the branch instead of silently selecting one.
5. Record missing decision points when the documentation commits the reader to a path before exposing meaningful alternatives.

## Documentation defect report

Create a structured finding whenever the journey reveals a documentation problem. Findings must be based on observed reader impact, not general stylistic preference.

Use these classifications where applicable:

- Missing prerequisite
- Missing decision point
- Incorrect instruction
- Broken command or reference
- Misleading wording
- Ambiguous instruction
- Incorrect ordering
- Missing scope
- Hidden dependency
- Duplicate or conflicting guidance
- Navigation problem
- Verification gap

For each finding, include:

- **ID:** sequential identifier such as `DOC-001`
- **Location:** file and heading path
- **Classification**
- **Observed behavior**
- **Reader impact**
- **Evidence from the journey**
- **Severity:** low, medium, high, or blocking
- **Suggested direction:** concise correction direction, not rewritten documentation

Do not inflate the report with cosmetic issues that did not affect the task.

## Recovery investigation

Begin recovery only after the reader-visible path fails, becomes incomplete, or does not expose the requested task.

When recovery begins:

1. State exactly why the documented path is insufficient.
2. Record the targeted search performed.
3. Record each source discovered outside the original path.
4. Explain whether the missing information was undiscoverable, poorly signposted, or contradictory.
5. In active mode, clearly separate recovery commands or edits from the documented procedure.
6. Continue logging chronologically.

## Required output

Write the following report to the configured report file and return a concise summary with its path. Use this structure unless the user requests another format:

```markdown
## Walkthrough target

- Requested task:
- Starting document:
- Mode: read-only | active

## Decisions and assumptions encountered

For each material choice:
- Decision or assumption
- Options visible at the time
- Selection
- Basis
- Effect on the path

## Documentation journey

### 1. <action or document>

- Source:
- Heading path:
- Trigger:
- Decision:
- Assumption:
- Action:
- Expected result:
- Actual or predicted result:
- Status:

**Plain-language interpretation:**

<Short paragraph explaining what this means to an engineer following the documentation, including what is clear or confusing and why they would continue, hesitate, or become blocked.>

### 2. <next action or document>

...

## Documented-path breakpoints

For each breakpoint:
- Where it occurred
- What the reader knew at that point
- What was missing, misleading, or incorrect
- Immediate consequence

## Recovery investigation

- Why recovery was necessary
- Searches or inspections performed
- Information discovered outside the original path
- Whether that information was reasonably discoverable
- Deviations from documented instructions

## Documentation defect report

### DOC-001: <title>

- Location:
- Classification:
- Observed behavior:
- Reader impact:
- Evidence:
- Severity:
- Suggested direction:

## Final state

- Task completed:
- Execution attempted:
- Verified behavior:
- Unverified behavior:
- Remaining blockers:

## Next action

State that no further action was taken. In read-only mode, offer an active walkthrough. In active mode, offer no additional execution unless a specific unresolved continuation remains.
```

## Completion criteria

The work is complete when:

- the chronological documentation path is preserved;
- each transition has a visible reason;
- each journey step includes a plain-language interpretation of the reader experience;
- decisions and assumptions are recorded when they occur;
- failures and recovery searches are not hidden;
- documentation defects are tied to observed reader impact;
- read-only and active behavior are clearly distinguished;
- verified and unverified outcomes are separated;
- the completed report is written to the configured path;
- and the report reflects what a real reader could discover progressively, not what became obvious after reading everything.
