---
name: review-process
description: Review how an in-progress issue is being worked; decide whether approach, decomposition, and evidence strategy need adjustment. Atomic process-review judgment independent of lifecycle automation.
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
- **Skill opportunity:** a new skill would materially reduce recurring friction,
  or observed behavior no longer matches an existing skill.

As a cheap part of each checkpoint review, record only what is observable and
useful about materially used skills, missing or irrelevant activation,
interventions, repeated approaches, rediscovery, permission friction, scope
drift, and similar process friction. Skill use alone is not evidence of a
deficiency; do not deep-review every loaded skill.

If that evidence suggests a substantive skill-definition problem rather than
executor noncompliance, ambiguous tasking, provider permission, or an isolated
failure, recommend or escalate a focused `skill-authoring` review. Otherwise
return no skill action. Do not duplicate the `skill-authoring` judgment model
here.

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

## Evidence

Prefer the smallest evidence surface that supports the judgment. Useful evidence
includes:

- the checkpoint diff for checkpoint review, and the cumulative workflow diff
  for promotion review;
- changed file paths;
- current tasking state;
- the concise executor or orchestrator handoff;
- compact current-state process notes.

Do not build a generated review report, and do not load the repository broadly to
feel thorough.

## Process State

Process review depends on process evidence that code and Git state do not carry.
Read the compact current-state process notes on the tasking rail; they exist so
a reviewer who did not do the work can still see avoidable interventions, repeated
approaches, and friction. Treat them as current state, not history.

If no process notes exist, review from the evidence that is available and say
plainly what was unobservable, so the next rail can carry it.

## Result

Return one of:

- concrete, justified process changes for the next checkpoint;
- explicit acknowledgment that no process change is warranted;
- process issues that should be escalated or deferred.

## ChatGPT Interaction

When ChatGPT intentionally activates this shared skill for substantive process
review, begin with `Skill: review-process` or, when composed by auto-review,
announce every materially active skill in responsibility order, such as
`Skills: auto-review → review-process`. Recommend an advisory reasoning level,
briefly summarize that the review will evaluate approach, decomposition,
intervention balance, evidence strategy, and skill opportunities, then ask
`Proceed?` and stop before substantial analysis until confirmation.

This instruction is scoped to ChatGPT use. It does not change Copilot or Work
review execution.

The gate is activation-time and occurs once per continuous process review. After
the user proceeds, begin follow-up responses with the active skill or chain
without repeating the reasoning cue, review summary, or proceed gate. Gate again
only for a new invocation, a materially changed chain, or a scope change that
makes a new reasoning decision meaningful.
