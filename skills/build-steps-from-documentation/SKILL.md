---
name: build-steps-from-documentation
description: >-
  Review project documentation to determine how to complete a requested task,
  identify the decisions, assumptions, alternatives, and prerequisites that
  affect which instructions apply, and produce sourced step-by-step instructions
  with file and heading references. Use when the user asks how to install,
  configure, deploy, operate, test, upgrade, recover, or otherwise perform a task
  according to project documentation.
---

# Build Steps From Documentation

Turn project documentation into a usable, sourced procedure. Expose the reasoning that determines which instructions apply instead of silently choosing a path.

## Core behavior

1. Identify the requested outcome before assembling steps.
2. Review enough relevant documentation to discover the supported approaches, decision points, prerequisites, and ordered instructions.
3. Unlike `simulate-documentation-following`, this skill may search and synthesize across relevant documentation because its goal is to produce the correct procedure rather than preserve a reader's limited-context journey.
4. Distinguish choices that determine the procedure from prerequisites that only determine whether execution can begin.
5. List material assumptions and decisions before presenting the procedure.
6. Do not silently select among materially different approaches such as:
   - single-node versus multi-node;
   - containerized versus direct installation;
   - development versus production;
   - highside versus lowside;
   - online versus air-gapped;
   - local versus remote execution;
   - supported platform or version variants.
7. Prefer an explicitly requested option. Otherwise use documented defaults, then strong repository or environment evidence, then the smallest reasonable assumption.
8. Label every non-explicit choice.
9. If an unresolved choice produces substantially different instructions, either present branches or identify the decision required before the procedure can be finalized.
10. Record prerequisites without blocking procedure discovery merely because they have not yet been verified.
11. Cite the source file for every step. Include the heading path when the document provides meaningful headings.
12. Explain why each step belongs in the sequence, especially when dependencies are not obvious.
13. Do not invent missing commands, values, defaults, or ordering. Mark gaps explicitly.
14. Do not claim successful execution unless the steps were actually performed and verified.

## Decision classification

Classify every material decision using one of these statuses:

- **Confirmed:** explicitly provided by the user
- **Documented default:** selected because the documentation declares it the default
- **Inferred:** selected from strong repository, environment, or surrounding context
- **Assumed:** selected to make progress despite insufficient evidence
- **Unresolved:** materially affects the procedure and cannot be selected responsibly

For each decision, include:

- available options;
- selected option, if any;
- classification;
- basis;
- effect on the resulting procedure;
- source, when documented.

## Determine what must be decided first

A decision belongs before the procedure when changing it would alter one or more of the following:

- which document or section applies;
- required components;
- command sequence;
- inventory or topology;
- configuration values;
- security boundary;
- deployment artifacts;
- validation method;
- cleanup or recovery path.

A prerequisite does not need to be resolved before producing instructions when it only answers whether a documented step can currently be executed. List it under prerequisites and state how to verify it.

## Source handling

For every step, provide:

- **Source file**
- **Heading path**, when available
- **Instruction basis:** direct instruction, combined instructions, documented implication, or necessary dependency

When a step is synthesized from multiple sources, list all contributing sources and explain the combination. Do not imply that a single document contained the complete instruction when it did not.

When documentation conflicts:

1. Identify the conflict.
2. Cite both sources.
3. Prefer an explicitly scoped or more authoritative source only when the documentation establishes that hierarchy.
4. Otherwise mark the step unresolved rather than choosing silently.

## Building the procedure

Order steps by actual dependency, not merely by file order.

Each step should contain:

- **Action:** what to do
- **Instructions:** commands or concrete changes
- **Reason:** why the step is required now
- **Source:** file and heading path
- **Instruction basis:** how the source supports the step
- **Applies when:** relevant decisions or variants
- **Prerequisites:** requirements for executing the step
- **Verification:** how to confirm the step worked, when documented or safely inferable

Keep instructions specific enough to execute, but do not expand into unrelated explanation or documentation redesign.

## Required output

Use this structure unless the user requests another format:

```markdown
## Requested outcome

<Concise statement of the task being documented.>

## Decisions and assumptions

### 1. <decision>

- Options:
- Selection:
- Status: Confirmed | Documented default | Inferred | Assumed | Unresolved
- Basis:
- Effect on instructions:
- Source:

## Prerequisites

For each prerequisite:
- Requirement
- Why it matters
- How to verify it
- Source
- Current verification status, if known

## Step-by-step instructions

### 1. <step title>

- Action:
- Instructions:
- Reason:
- Source file:
- Heading path:
- Instruction basis:
- Applies when:
- Prerequisites:
- Verification:

### 2. <step title>

...

## Documentation gaps or conflicts

Only include gaps, contradictions, or missing decisions that affect the requested procedure.

## Confidence and verification status

- Instructions directly documented:
- Instructions synthesized across sources:
- Assumptions still in effect:
- Unresolved decisions:
- Executed and verified:
- Not executed or unverified:
```

## Execution mode

When the user asks only for instructions, do not execute commands or modify files.

When the user asks to follow or test the instructions:

1. Preserve the decisions and assumptions section.
2. Execute the procedure in order when the environment permits it.
3. Record the observed result for each step.
4. Stop destructive or irreversible actions unless the user has clearly requested them.
5. Separate documented verification commands from additional checks introduced during execution.
6. Update the final verification status based only on observed evidence.

## Completion criteria

The work is complete when:

- the requested outcome is clear;
- material alternatives have been identified;
- every selected path is classified and justified;
- unresolved decisions are exposed rather than hidden;
- prerequisites are separated from path-selection decisions;
- every procedural step has a source file and heading path when available;
- the steps are ordered by dependency;
- documentation gaps and conflicts affecting the procedure are identified;
- and verification claims match the evidence actually observed.
