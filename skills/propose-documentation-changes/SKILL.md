---
name: propose-documentation-changes
description: >-
  Use when the user asks to correct, reorganize, or improve project documentation
  and a documentation walkthrough report or equivalent recorded findings are
  available. Use the recorded evidence to produce a file-by-file proposal
  describing what to add, move, remove, or rewrite before offering direct edits
  or parallel -proposed files.
---

# Propose Documentation Changes

Produce an evidence-based documentation change plan that makes the documented reader path lead reliably to the intended outcome. Do not rewrite documentation during the proposal phase.

## Purpose

Use this skill after one or more of the following are available:

- a documentation-following journey;
- a documentation defect report;
- a sourced procedure built from the documentation;
- observed execution failures;
- the current documentation files.

The proposal should reconcile the current documentation with the path a reader actually needs to follow.

## Report selection and handoff

Use the walkthrough report as the primary handoff artifact rather than relying on conversation memory alone.

1. Use an explicit report path supplied by the user.
2. Otherwise use a report path clearly established in the current conversation.
3. Otherwise inspect `./tmp/documentation-walkthrough-*.md` in the target project's current working directory.
4. If exactly one plausible report exists, use it.
5. If multiple reports are plausible or the intended report is otherwise ambiguous, ask the user which report to use before inspecting documentation or producing a proposal. Do not guess, merge reports, or begin a broad investigation.
6. Read the selected report, including its `User notes` section, before inspecting the documentation files it references.
7. Verify the referenced documentation in its current state before proposing changes so stale or already-resolved findings are not applied blindly.

Treat the evidence sources distinctly:

- walkthrough findings are observed evidence;
- `User notes` are explicit guidance, corrections, priorities, and constraints;
- current documentation is the present state against which proposed changes must be checked.

Do not erase a walkthrough finding merely because a user note rejects or reframes it. Record how the note affected the proposal, and identify changes driven by user notes separately from changes driven by observed defects.

## Core boundaries

1. Begin in proposal-only mode.
2. Do not edit, move, rename, create, or delete documentation files while developing the proposal.
3. Every proposed change must:
   - resolve an observed documentation defect;
   - align the documentation with a verified or sourced procedure;
   - expose a material decision or prerequisite at the correct point;
   - or improve navigation required to reach the requested outcome.
4. Do not propose cosmetic rewrites, broad style normalization, or unrelated cleanup unless the user explicitly requests them.
5. Preserve correct content and existing project terminology wherever possible.
6. Prefer the smallest coherent documentation change that fixes the reader journey.
7. Do not infer authorization to implement changes from a request to review or propose them.
8. Do not claim that proposed documentation will work unless the resulting path has been verified or the remaining uncertainty is clearly stated.

## Evidence requirements

Tie each proposed change to one or more of:

- documentation defect IDs such as `DOC-001`;
- a specific observed failure or reader breakpoint;
- a missing or misplaced prerequisite;
- a sourced procedure step;
- a documented conflict;
- an unresolved path-selection decision;
- an explicit user note.

If a suggestion lacks supporting evidence, omit it or label it optional and explain why it is outside the minimum required correction.

## File-level change types

Organize recommendations by file and use these categories:

### Add

Use for new content such as:

- scope statements;
- installation-path choices;
- prerequisites;
- verification steps;
- troubleshooting guidance;
- missing links or references;
- explanations required to understand a decision.

### Move

Use when content is valid but located where it commits readers to the wrong path, interrupts navigation, duplicates a canonical source, or belongs in a more specifically scoped document.

Always specify:

- source file and heading;
- destination file and intended heading;
- what remains behind, such as a summary and link;
- why the move improves the reader path.

### Rewrite

Use when the current content is misleading, ambiguous, incorrect, overly broad, or insufficiently scoped.

Always specify:

- exact location;
- current problem;
- intended meaning or behavior;
- boundaries the rewrite must preserve.

Do not draft replacement prose unless the user requests it or implementation is later authorized.

### Remove

Use only for content that is incorrect, obsolete, harmful, or duplicative after another source becomes canonical.

Always explain why deletion is safer than correction or relocation.

## Scope guidance

For substantial procedural and reference documents, consider whether the opening should clarify:

- what the document covers;
- what it does not cover;
- intended environment or audience;
- assumptions already in effect;
- where readers should go for other variants.

Do not force a formal `Scope` heading into tiny index pages or self-evident single-purpose files unless lack of scope caused an observed problem.

For procedural documents, consider these sections when supported by the evidence:

- Scope
- Before you begin
- Decisions you must make
- Procedure
- Verification
- Troubleshooting

These are not mandatory boilerplate. Recommend only the structure needed for the task and reader path.

## Proposed reader path

When navigation or organization is part of the problem, include a compact proposed path such as:

```text
README.md
→ Choose environment
→ Choose deployment method
→ Review method-specific prerequisites
→ Follow installation procedure
→ Run verification steps
→ Troubleshooting
```

Show decision points before the reader is committed to a branch.

## Required output

Use this structure unless the user requests another format:

```markdown
## Proposal basis

- Requested outcome:
- Walkthrough report:
- Evidence reviewed:
- User notes considered:
- Verified procedure available: yes/no
- Remaining uncertainty:

## Proposed reader path

<Current path and proposed path, when navigation changes are required.>

## Proposed documentation changes

### `<path/to/file>`

#### Add

- Change:
- Location:
- Content intent:
- Reason:
- Reader outcome:
- Evidence or issue IDs:
- Evidence source: walkthrough | user note | current documentation | sourced procedure
- Priority: required | optional

#### Move

- Content:
- From:
- To:
- Leave behind:
- Reason:
- Reader outcome:
- Evidence or issue IDs:
- Evidence source:
- Priority:

#### Rewrite

- Location:
- Current problem:
- Intended meaning or behavior:
- Constraints to preserve:
- Reason:
- Reader outcome:
- Evidence or issue IDs:
- Evidence source:
- Priority:

#### Remove

- Location:
- Content:
- Reason removal is appropriate:
- Replacement or canonical source:
- Evidence or issue IDs:
- Evidence source:
- Priority:

## Cross-file consistency changes

Only include changes required to keep links, terminology, prerequisites, defaults, or ordering consistent across files.

## Changes intentionally excluded

List adjacent cleanup or redesign ideas that were considered but excluded because they are unrelated, cosmetic, insufficiently supported, or outside scope.

## Verification plan

Explain how the revised documentation should be tested:
- entry point to completion;
- branch selection;
- prerequisites;
- commands or actions;
- expected outcomes;
- links and references;
- fresh-reader or clean-environment validation.

## Implementation options

No documentation files have been modified.

1. Apply the approved changes directly to the existing files.
2. Create parallel proposed copies using `<name>-proposed.<extension>` while leaving originals unchanged.
3. Leave the proposal as a report only.
```

## Implementation requires explicit selection

After producing the complete proposal, offer the three implementation options. Do not implement anything in the same step unless the user has already explicitly selected an option.

### Option 1: Direct edits

When the user selects direct edits:

1. Apply only approved proposal items.
2. Preserve unrelated wording, formatting, and structure.
3. Do not add opportunistic cleanup.
4. Avoid deleting or relocating content not covered by the approved proposal.
5. Report each modified file and the proposal items implemented.
6. Run available documentation formatting, link, lint, or validation checks.
7. Report validation evidence and any unverified behavior.
8. Leave uncertain changes unapplied and explain why.

### Option 2: Parallel proposed copies

When the user selects proposed copies:

1. Leave every original file unchanged.
2. Create a parallel file in the same directory by inserting `-proposed` before the final extension.
3. Examples:
   - `README.md` → `README-proposed.md`
   - `install.adoc` → `install-proposed.adoc`
   - `config.example.yaml` → `config.example-proposed.yaml`
4. Preserve the original directory structure.
5. Do not overwrite an existing proposed file without explicit authorization.
6. Apply only approved proposal items to the proposed copies.
7. Report every created file and the proposal items represented.
8. Run available validation against the proposed copies when practical.

### Option 3: Report only

Make no file changes. Leave the proposal ready for review or later implementation.

## Completion criteria

The proposal phase is complete when:

- the intended walkthrough report was selected without ambiguity or confirmed by the user;
- the selected report and its `User notes` were read before proposing changes;
- current documentation was checked against the report findings;
- every required change is organized by file;
- each change is categorized as add, move, rewrite, or remove;
- each change has a reason, reader outcome, evidence, evidence source, and priority;
- the proposed reader path exposes material decisions before branch-specific instructions;
- unrelated cleanup is excluded;
- a verification plan is included;
- and the user is offered direct edits, parallel proposed copies, or report-only handling.

The implementation phase is complete only when:

- the user explicitly selected an implementation option;
- only approved changes were made;
- originals were preserved when proposed copies were selected;
- modified or created files were reported;
- and validation results are separated from anything that remains unverified.
