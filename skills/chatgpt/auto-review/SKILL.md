---
name: auto-review
description: Decide and govern checkpoint or promotion review composition for AI Dev work, including ticket skill-candidate disposition and accepted skill remediation.
---

# ChatGPT Auto-Review

Own review policy and judgment for the current AI Dev lifecycle stage. This is
the decision-oriented side of auto-review. Copilot owns the deterministic
repository helpers and their execution.

## Contract

- Decide which configured candidate reviews apply from the evidence surface,
  issue scope, requirements, and current task state.
- Load or invoke only applicable review skills. A skipped candidate is not a
  failure when its non-applicability is justified.
- Define the review question and evidence needed for each applicable candidate.
- Synthesize concrete findings across applicable reviews.
- Decide whether findings are material and whether the result is `pass` or
  `action required`.
- Own escalation when a finding affects safety, lifecycle, scope, authority, or
  evidence sufficiency.
- Treat the active ticket's `Skill Candidates` and `Skills` sections as part of
  the review state for issue workflows.
- Authorize recording a promotion-review pass only after every applicable review
  passes, skill-investment state is closure-ready, and the current SHA and
  workflow identity are verified.

## Checkpoint Composition

For checkpoint review, apply `review-process`. It judges whether the approach,
decomposition, intervention balance, evidence strategy, or skills should change
for the next checkpoint and must reassess the ticket's current `Skill Candidates`
against the new checkpoint evidence.

Compose `skill-authoring` when process review identifies a substantive
skill-definition decision: creating a skill, refining an existing skill,
retiring a candidate with a final no-skill judgment, or otherwise requiring
focused skill-quality ownership. Do not load it merely because a candidate still
exists and the new evidence does not materially change its assessment.

Checkpoint review may pass with unresolved `Skill Candidates` when each has been
explicitly reassessed and evidence remains insufficient for a final disposition.
It must return `action required` when a candidate has been accepted into `Skills`
and the required create/refine implementation and dogfood work remains incomplete.
Accepted skill remediation is completed before the next named product checkpoint
begins, even when it produces additional Flow checkpoint commits.

A legacy ticket missing `Skill Candidates` or `Skills` is not proof of empty
skill state. Treat the missing sections as a normalization finding and establish
the smallest correct ticket state during checkpoint review.

An explicit "no process change is warranted" completes checkpoint review only
when candidate reassessment and any required skill remediation are also complete.

## Promotion Composition

For promotion review, apply `review-process` and decide whether
`frontend-design-review` applies from the changed-file and issue evidence before
loading it. Skip front-end review when the issue has no GUI or front-end design
work. All applicable candidates must pass.

Promotion review is the final skill-candidate gate for the issue. Compose
`skill-authoring` as necessary so that:

- every remaining `Skill Candidates` entry has a final disposition;
- final no-skill decisions are recorded explicitly enough to prevent later
  re-litigation;
- every accepted create/refine action is represented in `Skills`;
- every `Skills` entry is complete and has adequate implementation/dogfood
  evidence.

Do not authorize promotion while a candidate remains merely "reassess later," an
accepted skill investment is pending or in progress, or the required ticket skill
sections are absent/ambiguous.

Request or consume evidence gathered by Copilot's
`skills/copilot/auto-review/scripts/review-evidence --mode checkpoint|promotion`
before making applicability decisions. The helper gathers evidence, including
active ticket skill state when available; it does not make candidate or skill
judgments.

## Recording Boundary

A promotion pass is current-state evidence bound to the scratch SHA and active
workflow identity. Do not record a pass while any applicable review has a
material finding, while evidence is insufficient, while ticket skill state is
not closure-ready, or when the decision requires scope beyond the active issue.
Once the judgment is complete and authorized, the canonical recorder is:

`skills/copilot/auto-review/scripts/record-promotion-review`

ChatGPT authorizes that transition; it should not turn review policy into an
unbounded repository command-running loop.

## ChatGPT Interaction

When ChatGPT begins substantive review work with this skill, begin the
user-facing response with `Skill: auto-review`. If it intentionally composes
other review skills, announce every materially active skill in responsibility
order, for example:

`Skills: auto-review → review-process → skill-authoring`

Briefly summarize what the review will evaluate before continuing.

This instruction is scoped to ChatGPT use. Continue announcing the active skill
or chain in follow-up responses by beginning them with the active skill or
chain, without repeating the summary unless the active chain changes
materially.
