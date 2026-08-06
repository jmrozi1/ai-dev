---
name: review-documentation-for-task-propose-changes
description: >-
  Internal specialization for review-documentation-for-task. Use directly only
  when the user explicitly requests an evidence-based documentation change
  proposal for a task and a walkthrough, required procedure, comparison, or
  equivalent findings are available. Repair the complete reader path with the
  smallest coherent set of changes.
---

# Review Documentation For Task: Propose Changes

Produce a file-by-file documentation change proposal that makes the documented path reliably support the requested task.

## Role in the parent review

Use the shared task model, documentation inventory, architecture review, reader simulation, dependency-correct procedure, comparison findings, and user notes supplied by `review-documentation-for-task`.

Do not reduce the review to isolated defect patches. Preserve the end-to-end understanding of scope, decisions, routing, prerequisites, configuration, execution, verification, and recovery.

## Evidence selection

When invoked directly:

1. Use an explicit report path supplied by the user.
2. Otherwise use a report path clearly established in the current conversation.
3. Otherwise inspect `./tmp/documentation-walkthrough-*.md` in the target project.
4. Use the report when exactly one plausible match exists.
5. Ask the user when multiple reports are plausible. Do not guess, merge reports, or begin broad speculative investigation.

Read the selected report, including `User notes`, and inspect referenced documentation in its current state before proposing changes.

Keep evidence types distinct:

- walkthrough findings: observed reader evidence;
- task/procedure comparison: structural and dependency evidence;
- user notes: explicit guidance, corrections, priorities, and constraints;
- current documentation: present state;
- sourced procedure: required task sequence.

A user note may reframe or reject a finding, but does not erase the recorded reader experience. Explain how it affected the proposal.

## Core boundaries

1. Begin in proposal-only mode.
2. Do not modify documentation until the user explicitly selects an implementation option.
3. Prefer the smallest coherent set of changes that repairs the full reader path.
4. Preserve correct content and project terminology.
5. Exclude cosmetic normalization, unrelated cleanup, and speculative redesign.
6. Do not claim proposed documentation will work unless the revised path has been validated.

## Assigning responsibility

Propose changes at the earliest document that should have prevented the failure.

Examples:

- unclear applicability at entry: fix the entrypoint scope;
- supported method not discoverable: fix routing at the entrypoint or decision page;
- configuration appears after dependent commands: move or route configuration before execution;
- path-specific commands appear as general instructions: add or repair path selection and scoping;
- detailed instruction is locally wrong: fix the procedural document.

Do not place all fixes in the document where missing information was eventually found.

## Change categories

Organize changes by file using:

- Add
- Move
- Rewrite
- Remove

For each change include location, intended behavior, reason, reader outcome, evidence IDs, evidence source, priority, and relationship to the proposed reader path.

Use `Remove` only for incorrect, obsolete, harmful, or duplicative content after a canonical source is established.

## Proposed reader path

Show the repaired path from the actual entrypoint through completion. Expose material decisions before committing the reader to branch-specific instructions.

Typical conceptual order:

```text
Entrypoint and scope
→ supported paths and decisions
→ prerequisites
→ path-specific configuration
→ execution
→ verification
→ troubleshooting and recovery
```

Model changes around the project's current approach where practical, but do not preserve an organization that sends readers into invalid or inapplicable commands.

## Required output

```markdown
## Proposal basis

- Requested outcome:
- Selected path:
- Walkthrough report:
- Evidence reviewed:
- User notes considered:
- Required procedure available:
- Remaining uncertainty:

## Current reader path

## Proposed reader path

## Proposed documentation changes

### `<path>`

#### Add | Move | Rewrite | Remove
- Change or content:
- Location or source:
- Destination, when moving:
- Intended behavior:
- Reason:
- Reader outcome:
- Evidence or issue IDs:
- Evidence source: walkthrough | comparison | user note | current documentation | sourced procedure
- Priority: required | optional

## Cross-file consistency changes

## Changes intentionally excluded

## Verification plan

## Implementation options

1. Apply approved changes directly.
2. Create parallel `-proposed` copies.
3. Leave the proposal as a report only.
```

## Implementation

Implement only after explicit selection.

For direct edits:

- apply only approved changes;
- preserve unrelated wording and structure;
- run available documentation validation;
- report modified files and observed validation results.

For proposed copies:

- leave originals unchanged;
- insert `-proposed` before the final extension;
- do not overwrite an existing proposed file without authorization;
- validate the copies when practical.

## Completion criteria

The proposal is complete when the evidence source is unambiguous, current documentation has been checked, the whole reader path is repaired, each change is assigned to the earliest responsible document, every change is traceable to evidence, unrelated cleanup is excluded, a verification plan is included, and implementation remains explicitly optional.
