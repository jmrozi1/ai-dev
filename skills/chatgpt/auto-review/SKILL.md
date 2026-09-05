---
name: auto-review
description: Decide and govern checkpoint, promotion, or in-flight review composition for AI Dev work, including in-flight process review at a natural orchestrator handoff while a named checkpoint is still in progress, ticket skill-candidate disposition, and accepted skill remediation.
---

# ChatGPT Auto-Review

Own review policy and judgment for the current AI Dev lifecycle stage. This is
the decision-oriented side of auto-review. The executor audience owns the
deterministic repository helpers and their execution.

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

Request or consume evidence gathered by the executor before making
applicability decisions. Claude runs `ai-dev review-evidence --mode
checkpoint|promotion`, which invokes the canonical
`skills/copilot/auto-review/scripts/review-evidence` helper through supported
interpreter selection. The helper gathers evidence, including
active ticket skill state when available; it does not make candidate or skill
judgments.

## In-Flight Composition

In-flight process review is a third composition stage, distinct from checkpoint
and promotion review. It is bound to a handoff rather than a lifecycle boundary,
and it carries none of their recording, gating, or skill-disposition authority.

### Applicability

Consider in-flight review only when all of the following hold:

- the active named ticket checkpoint is still in progress;
- you are at a natural handoff, meaning the previous executor rail has ended in
  a published handoff, block, or failure and no executor is currently executing
  an authorized rail;
- you are about to issue the next rail for that same named checkpoint.

It never interrupts, pauses, or preempts an executor mid-rail, and never requires
a running executor to stop and self-review. It is additive: it never replaces,
defers, or satisfies checkpoint review or promotion review, never records a
review pass, and never advances the named roadmap or a Flow checkpoint.

### Evidence Basis

Decide from observable current process evidence only: the authorized rail,
published handoffs, current-state process notes, and lifecycle or repository
state. Material signals include repeated invalid controls or baselines; repeated
provenance or evidence invalidation; repeated reconstruction or repair of probes,
harnesses, fixtures, or measurement tooling; the evidence apparatus having become
the dominant work instead of advancing the named checkpoint; and successive
materially equivalent rails ending in the same class of unresolved obligation for
the same route reason. Weigh recurrence, cost, and whether the route or
measurement strategy itself is churning. This is a judgment, never a count.

Persistence of one proof obligation across several rails is not sufficient by
itself. It becomes a signal only when paired with observable churn or failure of
the route or measurement strategy. Correctly retiring invalid evidence and
correctly honoring stop conditions are good local discipline, not churn; the
signal is that the strategy for obtaining valid evidence keeps failing.

Never activate on elapsed time, wall-clock duration, prompt, turn, or exchange
count, token or credit usage, retry count, number of Flow checkpoint commits, any
score or dashboard metric, or the mere length or difficulty of work that is
steadily converging. A long checkpoint that keeps resolving real obligations,
narrowing the problem, or producing valid new evidence continues without review.
If a proposed rule could be evaluated by counting alone, it is outside this
stage.

### Action And Result

When it applies, compose `review-process` before issuing another materially
equivalent rail and ask one narrow question: on current process evidence, should
the approach, decomposition, executor rail, or evidence strategy change in order
to reach this checkpoint? Supply the smallest sufficient evidence surface, such
as the current rail authorization, recent handoffs, process notes, changed paths,
and lifecycle state. Build no report, monitor, or store.

`review-process` alone decides process quality here; this skill owns
applicability, timing, composition, evidence surface, and escalation, and does
not pre-judge, override, or substitute for that judgment. The review returns
concrete justified process changes, an explicit `no process change warranted`, or
process issues to escalate or defer. `no process change warranted` is a complete,
preferred result and permits reissuing the intended rail.

Carry material findings into the current tasking surface: orchestrator-owned
accepted state and authorized rail under a control plane, otherwise
`.ai-dev/tasking.md`. Do not immediately reissue a materially equivalent rejected
strategy unchanged; revisit it only with a stated reason grounded in new
evidence.

In-flight review may add a `Skill Candidates` entry when evidence warrants, but
it does not perform and does not satisfy the named-checkpoint or promotion
skill-candidate disposition gate.

### Violations

Each of the following breaches this stage: firing on long but steadily
converging work; relying on any fixed threshold; interrupting or preempting an
executor mid-rail; deciding process quality here or skipping `review-process`;
immediately reissuing a materially equivalent rejected strategy without carrying
findings forward; treating in-flight review as, or in place of, checkpoint or
promotion review, or recording a pass from it; firing on a single persistent
proof obligation with no route or measurement churn; and introducing a monitoring
service, timer, counter, scoring system, dashboard, transcript store, retry
framework, or new skill in order to implement it.

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
