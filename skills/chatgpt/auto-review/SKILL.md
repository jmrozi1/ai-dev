---
name: auto-review
description: Decide and govern checkpoint or promotion review composition for AI Dev work.
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
- Authorize recording a promotion-review pass only after every applicable review
  passes and the current SHA and workflow identity are verified.

## Checkpoint Composition

For checkpoint review, apply `review-process`. It judges whether the approach,
decomposition, intervention balance, evidence strategy, or skills should change
for the next checkpoint. An explicit "no process change is warranted" completes
that review.

## Promotion Composition

For promotion review, apply `review-process` and decide whether
`frontend-design-review` applies from the changed-file and issue evidence before
loading it. Skip front-end review when the issue has no GUI or front-end design
work. All applicable candidates must pass.

Request or consume evidence gathered by Copilot's
`skills/copilot/auto-review/scripts/review-evidence --mode checkpoint|promotion`
before making applicability decisions. The helper gathers evidence; it does
not make the review judgment.

## Recording Boundary

A promotion pass is current-state evidence bound to the scratch SHA and active
workflow identity. Do not record a pass while any applicable review has a
material finding, while evidence is insufficient, or when the decision requires
scope beyond the active issue. Once the judgment is complete and authorized,
the canonical recorder is:

`skills/copilot/auto-review/scripts/record-promotion-review`

ChatGPT authorizes that transition; it should not turn review policy into an
unbounded repository command-running loop.

## ChatGPT Interaction

When ChatGPT begins substantive review work with this skill, begin the
user-facing response with `Skill: auto-review`. If it intentionally composes
another review skill, announce the chain instead, for example:

`Skills: auto-review → review-process`

Then recommend an advisory reasoning level, briefly summarize what the review
will evaluate, and ask:

`Proceed?`

Stop before substantial review analysis until the user confirms. The
recommendation does not change or assume the actual ChatGPT reasoning setting.

This gate occurs once when the substantive review invocation begins. After the
user proceeds, continue announcing the active skill or chain in follow-up
responses by beginning them with the active skill or chain, without repeating
the reasoning cue, scope summary, or proceed gate.
Gate again only when a new substantive invocation begins, the active chain
changes materially, or the scope changes enough to make a new reasoning
decision meaningful.
