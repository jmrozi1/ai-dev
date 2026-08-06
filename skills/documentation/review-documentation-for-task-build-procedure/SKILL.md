---
name: review-documentation-for-task-build-procedure
description: >-
  Internal specialization for review-documentation-for-task. Use directly only
  when the user explicitly asks for the dependency-correct procedure supported
  by project documentation. Identify material path decisions, prerequisites,
  configuration, execution, verification, conflicts, and sources without
  silently choosing among materially different procedures.
---

# Review Documentation For Task: Build Procedure

Build the correct sourced procedure for a requested task from the relevant documentation. This establishes the dependency-ordered baseline used by the parent review.

## Role in the parent review

This specialization answers what the documentation collectively requires, not what a progressively reading engineer happens to encounter. It may search and synthesize across relevant documentation.

Use the task model from `review-documentation-for-task` when available. Do not replace or narrow that model without explicit evidence.

## Core behavior

1. State the requested outcome.
2. Identify every decision that materially changes the procedure.
3. Separate path decisions from prerequisites, configuration, execution, verification, troubleshooting, and recovery.
4. Do not produce a single path while materially different choices remain hidden.
5. Ask the user when an unresolved choice substantially changes applicable documents, commands, configuration, topology, security boundary, or validation.
6. Order all steps by actual dependency rather than file order.
7. Cite the source file and heading path for every step.
8. Mark synthesized instructions and list all contributing sources.
9. Identify conflicts, missing information, undocumented dependencies, and required ordering not expressed by the documentation.
10. Do not invent commands, values, defaults, or successful outcomes.

## Decision classification

Classify each material decision as:

- Confirmed
- Documented default
- Inferred
- Assumed
- Unresolved

For each decision record options, selection, status, basis, effect on the procedure, and source.

A choice belongs before the procedure when changing it alters applicable documentation, required components, configuration, command sequence, inventory, topology, security boundary, artifacts, verification, or recovery.

## Dependency model

Construct the procedure using this conceptual order where applicable:

1. Scope and applicability
2. Path-selection decisions
3. Prerequisites
4. Path-specific configuration
5. Execution
6. Verification
7. Troubleshooting and recovery

Do not force every document to use these headings, but use this dependency model to detect when required information is introduced too late.

For each execution step identify all preceding decisions, prerequisites, and configuration on which it depends. If the current documentation presents the action before any dependency, record that mismatch for the parent comparison.

## Source handling

For every step include:

- Action
- Instructions
- Reason and dependency position
- Source file
- Heading path
- Instruction basis: direct | combined | documented implication | necessary dependency
- Applies when
- Prerequisites
- Required configuration
- Verification

When documentation conflicts, cite both sources and use an explicit documented authority hierarchy only when one exists. Otherwise leave the conflict unresolved.

## Required output

Produce:

```markdown
## Requested outcome

## Material decisions

## Supported paths

## Prerequisites

## Required configuration

## Dependency-ordered procedure

### 1. <step>
- Action:
- Instructions:
- Reason:
- Source file:
- Heading path:
- Instruction basis:
- Applies when:
- Decisions already required:
- Prerequisites:
- Configuration already required:
- Verification:

## Verification and recovery

## Documentation gaps, conflicts, and ordering mismatches

## Confidence and verification status
```

## Boundaries

- Do not execute or modify files unless explicitly requested.
- Do not redesign documentation in this specialization.
- Do not conceal that a procedure was synthesized from disconnected or unreachable documents.
- Do not treat the presence of information somewhere in the repository as proof that a reader can discover it.

## Completion criteria

The procedure is complete when material paths are explicit, unresolved choices are exposed, decisions and dependencies precede affected actions, every step is sourced, conflicts and synthesis are visible, and verification claims match observed evidence.
