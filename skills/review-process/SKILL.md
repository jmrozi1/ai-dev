---
name: review-process
description: Review how an in-progress issue is being worked; decide whether approach, decomposition, evidence strategy, and reusable skill investment need adjustment. Atomic process-review judgment independent of lifecycle automation.
---

# Process Review Skill

Own process review judgment for development work.

This skill reviews how an issue is being worked and decides whether the next checkpoint
should be worked differently.

Review is independent and atomic: you can invoke it alone without lifecycle automation
or load composition. The review machinery in `auto-review` uses this skill in its
checkpoint and promotion lifecycle stages.

## Checkpoint Process Review

Checkpoint process review is frequent and cheap; it focuses on how the work is going,
not code correctness.

Assess how the current issue is being worked and decide whether the next checkpoint
should be worked differently. Look for these failure modes:

- **Approach quality:** the work is not converging, or decomposition and the
  evidence/verification strategy no longer match the real shape of the work.
- **Intervention balance:** unnecessary human confirmations or handoffs, or a
  leash that is too short (needless stops on settled decisions) or too long
  (executor settled product, scope, or architecture questions that should have
  stopped).
- **Wasted effort:** avoidable rediscovery, repeated searching, re-reading known
  state, or repeated failed approaches that reveal a process problem rather than
  a bug.
- **Skill opportunity:** a new or refined skill could materially reduce recurring
  friction, preserve reusable operational knowledge, or make observed behavior
  match the intended process.

As a cheap part of each checkpoint review, record only what is observable and
useful about materially used skills, missing or irrelevant activation,
interventions, repeated approaches, rediscovery, permission friction, scope
drift, and similar process friction. Skill use alone is not evidence of a
deficiency; do not deep-review every loaded skill.

### Skill Candidates

The active ticket's `Skill Candidates` section is the durable hypothesis surface
for reusable skill investment discovered while working that ticket.

At every named checkpoint review:

- read the current `Skill Candidates` and `Skills` sections when an active ticket
  exists;
- use current process evidence and tasking process notes to add a new candidate
  only when the observation may plausibly deserve reusable skill work;
- reassess every unresolved candidate against the new checkpoint evidence;
- keep a candidate when evidence is still insufficient for a final disposition;
- retire it with a concise `no skill` disposition when evidence shows the issue
  is isolated, adequately handled elsewhere, or not a skill-definition problem;
- escalate to `skill-authoring` when the evidence supports a substantive
  create/refine/no-action decision rather than a merely provisional candidate.

Do not use a numeric threshold such as a required observation count. Candidate
maturity is a judgment about recurrence, cost, risk, and reusability. One severe
failure can be sufficient evidence; many tiny annoyances may still warrant no
skill.

Keep candidates compact. Preserve the originating checkpoint or observation,
the material evidence, and current assessment only far enough to support later
reassessment. Do not turn the ticket into an investigation transcript.

### Accepted Skills

When `skill-authoring` accepts a candidate for skill creation or refinement, move
that investment into the ticket's `Skills` section. Record the skill or capability
being changed, the originating candidate/checkpoint, the accepted action, and its
current implementation/dogfood result.

Accepted skill work is checkpoint-review remediation. Complete and dogfood it
against the friction that justified the investment before advancing to the next
named product checkpoint. The skill work may create additional Flow checkpoint
commits without advancing the ticket's named roadmap. If the work cannot be
completed because of a real permission, scope, dependency, or repository-boundary
blocker, escalate it rather than silently moving on.

If the accepted skill is owned by another repository, keep the originating ticket
as the durable accounting surface and record the owning repository/change
reference and dogfood result there. Do not duplicate the canonical skill into the
product repository merely to keep the work local.

If evidence suggests a substantive skill-definition problem rather than executor
noncompliance, ambiguous tasking, provider permission, or an isolated failure,
recommend or escalate a focused `skill-authoring` review. Otherwise return no
skill action. Do not duplicate the `skill-authoring` judgment model here.

Do not turn checkpoint review into an implementation or code review. Inspect
changed code only far enough to judge process, and stop there.

Produce a small number of concrete, justified actions, each naming the
observation that motivated it. An explicit "no process change is warranted" is a
complete and preferred result when the work is going well. Do not manufacture
actions to make the review look productive, and do not narrate the checkpoint.

## Promotion Process Review

Promotion process review is less frequent and more comprehensive. It assesses
process quality over the cumulative issue and feeds into the promotion review
decision.

Assess:

- convergence and approach quality over the issue;
- human intervention and executor leash balance;
- avoidable rediscovery, repetition, and context waste;
- failed or repeated approaches worth remembering;
- decomposition and evidence-strategy problems;
- skills and reusable knowledge discovered;
- explicit `no process change warranted` when the process went well.

Before promotion review can pass, perform a final skill-candidate disposition:

- every remaining `Skill Candidates` entry must either have an explicit final
  `no skill` disposition or be promoted through `skill-authoring` into `Skills`;
- no accepted `Skills` entry may remain pending or in progress;
- accepted skill work must include sufficient implementation and dogfood evidence
  to show the originating friction was actually addressed.

Missing `Skill Candidates` or `Skills` sections on a legacy active ticket are a
normalization finding, not evidence that no candidates or skills exist. Add the
smallest correct sections before promotion instead of treating absence as empty
state.

## Evidence

Prefer the smallest evidence surface that supports the judgment. Useful evidence
includes:

- the checkpoint diff for checkpoint review, and the cumulative workflow diff
  for promotion review;
- changed file paths;
- current tasking state;
- the concise executor or orchestrator handoff;
- compact current-state process notes;
- the active ticket's `Skill Candidates` and `Skills` sections;
- the compact AI usage report when an issue usage summary is available. Treat
  usage as a process signal, not a verdict: high usage alone does not prove
  inefficiency, and low usage alone does not prove good execution;
- unavailable or unattributable usage is not zero. Usage variance matters only
  when actual and expected values use compatible provider-native units and
  scope. Process conclusions about complexity, rediscovery, decomposition,
  intervention, or skills require supporting process evidence.

An explicit `no process change is warranted` remains valid when the available
usage and process evidence do not support a change. Do not duplicate the
management renderer's formatting rules here.

Do not build a generated review report, and do not load the repository broadly to
feel thorough.

## Process State

Process review depends on process evidence that code and Git state do not carry.
Read the compact current-state process notes on the tasking rail; they exist so
a reviewer who did not do the work can still see avoidable interventions, repeated
approaches, and friction. Treat them as unclassified current observations, not
history or the canonical skill-candidate ledger.

Once checkpoint review classifies an observation into the ticket's `Skill
Candidates` or `Skills` section, remove the duplicate classified observation from
tasking process notes on the next rail rewrite. The ticket owns reviewed skill
investment state; the tasking file carries only current execution observations
needed for the next review.

If no process notes exist, review from the evidence that is available and say
plainly what was unobservable, so the next rail can carry it.

## Result

Return one of:

- concrete, justified process changes for the next checkpoint;
- explicit acknowledgment that no process change is warranted;
- process issues that should be escalated or deferred.

A checkpoint review may pass with unresolved `Skill Candidates` when each has
been explicitly reassessed and evidence is still insufficient for a final
skill-authoring disposition. It must not pass while newly accepted `Skills` work
that blocks advancement remains unresolved.

Completing a named ticket checkpoint is the normal boundary for creating a Flow
checkpoint commit and running checkpoint review. Review fixes or skill
remediation may create additional Flow checkpoint commits without advancing the
named roadmap.

## ChatGPT Interaction

When ChatGPT intentionally activates this shared skill for substantive process
review, begin with `Skill: review-process` or, when composed by auto-review,
announce every materially active skill in responsibility order, such as
`Skills: auto-review → review-process`. Briefly summarize that the review will
evaluate approach, decomposition, intervention balance, evidence strategy, and
skill opportunities before continuing.

This instruction is scoped to ChatGPT use. It does not change Copilot or Work
review execution.

Begin each response with the active skill or chain and continue without extra
activation gating. Re-announce the skill or chain only when the invocation or
composition changes materially.
