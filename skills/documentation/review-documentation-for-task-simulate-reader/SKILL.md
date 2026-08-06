---
name: review-documentation-for-task-simulate-reader
description: >-
  Internal specialization for review-documentation-for-task. Use directly only
  when the user explicitly requests a progressive reader walkthrough of project
  documentation. Preserve the reader's limited context, record failures and
  assumptions chronologically, and write an ephemeral walkthrough report.
---

# Review Documentation For Task: Simulate Reader

Simulate an engineer following documentation from the selected entrypoint. Preserve the journey as experienced instead of reconstructing an ideal path afterward.

## Role in the parent review

This specialization validates one concrete documented path. It does not own the overall documentation architecture, required-procedure model, or final change proposal.

Use the task model and selected path supplied by `review-documentation-for-task`. Do not silently choose among materially different environments, installation methods, targets, or variants.

## Modes

### Read-only

Default unless execution is explicitly requested.

- Do not run commands or change environment state.
- Do not edit project files except the generated walkthrough report.
- Predict likely results from the documentation and visible repository state.
- State clearly that execution was not attempted.

### Active

Use only when the user explicitly asks to perform or test the documented actions.

- Execute in documented order.
- Record exact actions, expected results, and observed results.
- Separate documented actions from recovery actions.
- Avoid destructive or irreversible actions unless clearly authorized.
- Never claim success without observed verification.

## Progressive-reading rules

1. Start from the entrypoint selected by the parent review, or the primary `README.md` when invoked directly without another entrypoint.
2. Do not preload all documentation or globally search for the final answer.
3. Follow links, references, decisions, prerequisites, and commands in the order encountered.
4. Before opening another document, record what in the current document caused that transition.
5. Record what the reader knows when each decision or action appears.
6. Do not use later discoveries to make earlier choices appear obvious.
7. When the path is insufficient, mark the breakpoint before beginning a narrow recovery search.
8. Do not hide false starts, misleading instructions, unreachable documents, or required outside knowledge.

## Required checks at each action

For every action presented as executable, determine:

- whether the document's scope makes the action applicable to the selected path;
- whether all material path decisions have already been resolved;
- whether required prerequisites and configuration have already been introduced;
- whether the reader can identify the command's purpose and expected effect;
- and whether the next required information is discoverable from the current path.

Flag a defect when documentation encourages execution before required configuration, prerequisites, or path selection are established. Flag the earliest document that should have prevented the problem, even when the missing information is discovered later.

## What to log

For each meaningful transition or action:

- Source and heading path
- Trigger
- Information available to the reader
- Decision and basis
- Assumption
- Action
- Expected result
- Actual or predicted result
- Status: continued | ambiguous | blocked | failed | recovered | verified
- Plain-language interpretation

The plain-language interpretation should briefly explain what a normal engineer would think at that moment, what is clear or confusing, and why they would continue, hesitate, or become blocked.

## Defect classifications

Use observed reader impact, including:

- Missing or unclear scope
- Unreachable path
- Missing decision point
- Missing prerequisite
- Hidden dependency
- Configuration after dependent execution
- Incorrect ordering
- Branch-specific instruction presented generally
- Broken command or reference
- Ambiguous or misleading instruction
- Navigation problem
- Verification gap
- Duplicate or conflicting guidance

For each defect include ID, location, observed behavior, reader impact, evidence, severity, suggested direction, and earliest responsible document.

## Report file

- Use a user-supplied path when provided.
- Otherwise write `./tmp/documentation-walkthrough-<task-slug>.md` relative to the target project.
- Create `./tmp/` when needed.
- Overwrite the same task-derived report by default.
- Treat the report as ephemeral evidence.

## Report structure

Include:

1. Walkthrough target and mode
2. Selected path and known decisions
3. Chronological documentation journey
4. Documented-path breakpoints
5. Recovery investigation
6. Documentation defect report
7. Report summary
8. User notes

The report summary must include an A-F rating, defects, ambiguities, assumptions, recovery requirements, verified and unverified behavior, blockers, and reasons for any rating below A.

Use a strict A standard: a competent engineer can discover and follow the selected path without undocumented assumptions, meaningful ambiguity, recovery searches, broken navigation, known defects, or actions appearing before their dependencies.

Preserve an editable final section:

```markdown
## User notes

<Optional user guidance, corrections, priorities, constraints, rejected findings, or additional context for the later proposal.>
```

## Completion criteria

The simulation is complete when the chronological path is preserved, every action is checked against scope and prior dependencies, defects are assigned to the earliest responsible document, the report includes plain-language interpretations and a justified rating, and verified behavior is separated from prediction.
