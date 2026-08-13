---
name: review
description: Use when reviewing an in-progress issue at a checkpoint to decide whether the next checkpoint should be worked differently, or when deciding whether cumulative workflow changes justify promotion to main.
---

# Review Skill

Own review judgment for development work. This skill has two modes: checkpoint
review and promotion review. Both optimize for relevant evidence rather than
maximal context, and both may legitimately conclude that no action is warranted.

## Choose The Mode

Select the mode from the task context:

- Use **checkpoint review** when a unit of work has reached a checkpoint and the
  question is how the next checkpoint should be worked.
- Use **promotion review** when the question is whether cumulative workflow
  changes justify promotion to the main branch.

If the task context does not make the mode obvious, ask rather than performing
both. Running both modes by default wastes the cost the modes exist to separate.

## Checkpoint Review

Checkpoint review is frequent, cheap, and about process, not code.

Assess how the current issue is being worked and decide whether the next
checkpoint should be worked differently. Look for these failure modes:

- **Approach quality:** the work is not converging, or decomposition and the
  evidence/verification strategy no longer match the real shape of the work.
- **Intervention balance:** unnecessary human confirmations or handoffs, or a
  leash that is too short (needless stops on settled decisions) or too long
  (executor settled product, scope, or architecture questions that should have
  stopped).
- **Wasted effort:** avoidable rediscovery, repeated searching, re-reading known
  state, or repeated failed approaches that reveal a process problem rather than
  a bug.
- **Skill opportunity:** a new skill would materially reduce recurring friction,
  or observed behavior no longer matches an existing skill.

Do not turn checkpoint review into an implementation or code review. Inspect
changed code only far enough to judge process, and stop there.

Produce a small number of concrete, justified actions, each naming the
observation that motivated it. An explicit "no process change is warranted" is a
complete and preferred result when the work is going well. Do not manufacture
actions to make the review look productive, and do not narrate the checkpoint.

## Promotion Review

Promotion review is less frequent, more comprehensive, and about whether
promotion is justified. It reviews the cumulative workflow, not one checkpoint.

Assess:

- current requirements and the evidence that they are satisfied;
- implementation correctness across the cumulative change;
- tests and other validation, including whether the evidence chosen fits the
  risk;
- coding and project standards relevant to the changed surface;
- scope discipline and unintended behavioral change;
- unresolved failures, skipped work, and known limitations;
- technical debt that materially raises the cost of future change;
- human-facing documentation that the change made stale;
- skills the change made stale, and reusable knowledge discovered during the
  issue that should become or refine a skill;
- process quality over the issue;
- whether promotion is actually justified.

Conclude with either `pass` or `action required`, and state the specific
required actions when action is required. Promotion review is intended to become
a hard prerequisite for promotion; that enforcement does not exist yet, so for
now the review happens because the process requires it, not because a command
refuses without it.

When a promotion review passes, record that state in `.ai-dev/promotion-review.json`
using the sibling helper in `skills/review/scripts/record-promotion-review`. The
record is current-state evidence only: it stores the pass result, the reviewed
workflow identity, and the current scratch commit SHA. It does not accumulate a
review history. The normal AI Dev default is that promotion review is required
before promotion unless a repository explicitly opts out.

Explicitly rejected in promotion review:

- repository-wide ingestion by default; read the changed surface and only what
  judgment genuinely requires beyond it;
- speculative cleanup or refactoring because a cleaner shape is imaginable;
- inventing work merely because improvement is possible.

A promotion review that finds nothing requiring action should say so plainly.

## Evidence

Prefer the smallest evidence surface that supports the judgment. Useful evidence
includes:

- the checkpoint diff for checkpoint review, and the cumulative workflow diff
  for promotion review;
- changed file paths;
- the current issue and the requirements the work must satisfy;
- test and validation results;
- current workflow state;
- current tasking state;
- the concise executor or orchestrator handoff;
- skills relevant to the changed surface.

Run this package's `scripts/review-evidence` first. It takes
`--mode checkpoint|promotion`, resolves the repository from the current working
directory, reads authoritative workflow state from `.ai-dev/workflow.json`, and
prints that state, the changed-file and stat surface for that mode, uncommitted
state, the rail's process notes, and the skills that need review. Read further
only where judgment genuinely requires it, using Flow commands and Git
inspection.

Do not build a generated review report, and do not load the repository broadly to
feel thorough. The expensive part of review is judgment, not rediscovery.

## Process State

Checkpoint review depends on process evidence that code and Git state do not
carry. Read the compact current-state process notes on the tasking rail; they
exist so a reviewer who did not do the work can still see avoidable
interventions, repeated approaches, and friction. Treat them as current state,
not history.

If no process notes exist, review from the evidence that is available and say
plainly what was unobservable, so the next rail can carry it.

## Skill Freshness

A change to a skill package's own contents brings that skill into review; check
whether its instructions still match its behavior. Skills also depend on files
and processes outside their own package, and a change there can leave a skill
silently stale.

A skill declares such a dependency with a `## Review Triggers` section listing
repository-relative paths or globs, one per bullet. When a changed path matches,
`scripts/review-evidence` reports that the skill needs review and why. Keep the
list short and local to the skill; it is not a dependency graph, a manifest, or
versioned metadata, and it is not resolved transitively.

## Review Triggers

- ai_dev_flow/workflow_state.py
