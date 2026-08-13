---
name: auto-review
description: Lifecycle review orchestration and candidate-skill composition. Owns checkpoint/promotion stage selection, applicability decisions, evidence gathering, promotion-pass recording, and interaction with the promotion gate.
allowed-tools: shell
---

# Auto-Review Skill

Own review lifecycle orchestration and deterministic review machinery.

This skill orchestrates candidate atomic review skills based on lifecycle stage
(checkpoint vs promotion) and decides applicability of each candidate.

Leaf review skills remain atomic and independently invokable. This skill composes
them into checkpoint and promotion review workflows without introducing a generic
dependency framework or naming-convention loader.

## Lifecycle Stages

### Checkpoint Review

Checkpoint review assesses whether the current approach and process should change
for the next checkpoint.

When selecting checkpoint review:

1. Run `scripts/review-evidence --mode checkpoint` to gather evidence.
2. Load and invoke `review-process` as the checkpoint candidate.
3. Return the process review result.

Checkpoint review is frequent and cheap.

### Promotion Review

Promotion review assesses whether cumulative changes justify promotion to main
and records a pass when appropriate.

When selecting promotion review:

1. Run `scripts/review-evidence --mode promotion` to gather evidence.
2. Identify configured promotion candidates.
3. For each candidate:
   - Decide applicability based on evidence surface (changed files, issue scope,
     requirements);
   - Skip if not applicable (concise reason);
   - Load and invoke if applicable.
4. When the promotion-review result is `pass`, record that state using
   `scripts/record-promotion-review` against the current scratch commit and
   workflow identity.
5. Return the aggregated promotion review result.

Promotion review is less frequent and more comprehensive.

## Candidate Composition

For this iteration, candidate profiles are:

```
checkpoint:
  review-process

promotion:
  review-process
  frontend-design-review
```

This configuration is owned by `auto-review` and should not be discovered by
naming convention. Later candidates should be added only when real use earns
them.

## Applicability Decisions

Configured reviews are candidates, not unconditional loads.

Before loading a candidate, decide whether it applies to the current work using
the smallest relevant evidence surface:

- changed files and file types;
- issue scope and requirements;
- tasking and process notes;
- candidate skill catalog description;
- prior lifecycle judgment if available.

Avoid loading the skill merely to discover non-applicability. A skill's catalog
description may be sufficient to decide that it does not apply.

For this issue:

- `review-process` applies at both stages (process judgment is always relevant).
- `frontend-design-review` applies at promotion only if the issue contains
  relevant GUI/front-end design changes. For Issue #34 itself, skip as not
  applicable (no UI changes).

Skipped reviews are not failures.

When a skipped candidate does not apply, report a concise reason so the
lifecycle review aggregation can distinguish between:

- "all applicable reviews passed";
- "applicable reviews passed; other candidates were skipped as not applicable".

## Deterministic Evidence and Recording

Run this package's `scripts/review-evidence` before loading candidate reviews.

The `review-evidence` script:

- takes `--mode checkpoint|promotion`;
- resolves the repository from the current working directory;
- reads authoritative workflow state from `.ai-dev/workflow.json`;
- prints workflow state, changed-file and stat surface, uncommitted state,
  tasking process notes, and skills that need review;
- remains read-only and deterministic.

When a promotion review result is `pass`, use `scripts/record-promotion-review`
to record the pass state in `.ai-dev/promotion-review.json` with:

- version and result fields;
- current scratch commit SHA;
- current workflow identity (main/scratch branch, issue number or patch
  description);
- no accumulation of review history.

This record gates the subsequent `flow-promote` command. Any committed change
after the pass invalidates it. The pass record is current-state evidence only.

## Promotion Gate Interaction

Preserve the promotion gate implemented in Issue #32:

- required by default in normal AI Dev operation;
- explicit opt-out via `.ai-dev/config.json` with `review.promotionGate: false`;
- pass bound to current scratch SHA and workflow identity;
- Flow enforces only the deterministic gate state.

Do not redesign the gate. Update paths and references only as needed because
review machinery moves to this package.

## Shared Skill Freshness

A skill declares external dependencies with a `## Review Triggers` section in
its SKILL.md, listing repository-relative paths or globs.

When a changed path matches, `scripts/review-evidence` reports that the skill
needs review.

This is lightweight, local to each skill, and not a global dependency graph.

## Result Aggregation

Checkpoint result:

- return the `review-process` result directly;
- if `review-process` returns "no process change warranted", checkpoint review
  is complete.

Promotion result:

- all applicable candidates must result in `pass` or appropriate action;
- skipped candidates do not prevent promotion pass;
- promotion review result is `pass` or `action required` with specific actions
  listed.

## Out of Scope

- Generic skill dependency metadata or graphs;
- Recursive dependency resolution;
- Automatic loading by naming convention;
- Review types not yet earned by real use;
- Redesigning the Flow promotion gate or creating a public `flow-review` command;
- Changing the SHA-keyed promotion gate model.

## Review Triggers

- ai_dev_flow/workflow_state.py
- skills/review-process/SKILL.md
- skills/frontend-design-review/SKILL.md
