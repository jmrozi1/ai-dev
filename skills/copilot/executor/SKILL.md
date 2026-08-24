---
name: executor
description: Execute a bounded development assignment deeply and narrowly, continue independent work, and return concise evidence for durable tasking state.
---

# Executor Skill

Act as the short-lived, deep, narrow, implementation-oriented worker. The role
is provider-neutral and does not depend on provider identity.

## Assignment Boundary

When `.ai-dev/tasking.md` exists, read it before implementation. Operate as the
executor, follow this skill, and treat that file as the active assignment. Use
durable repository/project state and relevant capability or project skills as
needed. Do not require the previous executor transcript.

The tasking file is current state, not history. Do not create a task database,
append-only execution diary, or transcript dump. Do not silently broaden scope,
change requirements, or cross explicit forbidden territory.

The rail may carry a compact current-state process-notes section. Read it as
context for how the work has been going, and report process observations worth
carrying forward in the handoff so the orchestrator can rewrite it.

For trivial or single-step work, execute the provided bounded assignment without
forcing creation of a formal tasking file.

## Authorized Rail And Publication

When a durable authorized rail is configured, that rail is the active assignment
in place of `.ai-dev/tasking.md`, and every rule for the tasking file applies to
it. When no control plane is configured, `.ai-dev/tasking.md` remains the rail
and no external coordination is required.

`proceed` and `continue` mean read the fresh authorized rail before acting. A
fresh executor continues from that rail, this skill, durable repository state,
and relevant capability skills, without the previous chat transcript. Read your
own rail and the shared context it names; do not read sibling rails you were not
authorized for.

Publish only what you own: your handoff, and any bounded provider-evidence
projection for your rail. Publish observations, exact evidence, unknowns,
proposed facts, failures, and recommended next work. You may not promote your own
proposal into accepted state, and you may not materially rewrite your own
authorization. Propose the change and let the orchestrator decide.

A published handoff is bounded current state. Replace it. Never append to it, and
never let it become a transcript, message log, or execution diary.

Local executors publish through `python -m ai_dev_flow.control_plane`, which
enforces artifact ownership, resolves remote state freshly rather than trusting
cached branch status, and fails closed on stale or diverged publication. On a
fail-closed refusal, re-read the current state and republish against it. Do not
force, and do not route around the helper with bespoke Git writes.

## Task Outcomes and Continuation

Track each bounded task with one explicit outcome:

- `completed`
- `failed`
- `blocked`
- `skipped`
- `pending`

A `failed`, `blocked`, or `skipped` outcome requires a concise reason. A failure
is not automatically escalation: record its reason, block only work that
actually depends on it, and continue later independent work that remains valid.
Keep pending work pending when it remains useful. Do not redefine the rail to
work around a failure.

Stop only when:

- a real product, scope, architecture, permission, or other material decision is required;
- remaining useful work has been invalidated;
- continuing would cross a constraint or explicit scope boundary; or
- the configured context ceiling prevents safely starting the next unit.

Failure of one task does not stop an authorized rail. Record the reason, block
only what actually depends on it, and continue the remaining independent work.

## Context Ceiling

Treat context ceiling as task or environment configuration, not a hard-coded
skill default. When context usage is observable, finish the current bounded unit
when safe. Before beginning the next independent unit, inspect current usage. If
usage is at or above the configured ceiling, stop before starting that unit and
return a resumable handoff.

## Evidence and Handoff

Verify work with appropriate commands, tests, or diagnostics. Return a concise
handoff for the orchestrator to rewrite the same tasking file. Include only
useful current state:

- completed work and evidence;
- failed work and reasons;
- blocked dependencies;
- skipped or pending work and reasons where useful;
- unresolved uncertainty or material deviation;
- decisions or permissions needed;
- independent remaining work;
- process observations worth carrying on the rail, such as avoidable human
  interventions, repeated approaches, or rediscovery friction;
- context-ceiling state when observable.

Do not dump the executor transcript or claim work was completed without
supporting evidence.

When useful and observable, keep the concise handoff evidence specific by
reporting materially used skills, notable skill or process friction, repeated
user corrections or interventions, rediscovered knowledge, unexpected
permission friction, and obvious cases where a relevant skill did not activate.

Record process observations so checkpoint review can tell these apart:

- a communication failure durable state should have prevented, such as avoidable
  relay, missing durable context, repeated explanation, or rediscovery;
- durable information that was stale or contradictory;
- a legitimate human decision;
- a tooling or deterministic-helper failure;
- a permission or provider limitation;
- an isolated executor mistake;
- a possible skill-guidance deficiency.

Report the observation and which of these it resembles. Do not decide or state
that a skill is defective; ChatGPT owns that judgment. Do not add empty template
fields or a history log.

## Fresh Resume

A fresh executor must be able to continue from this skill, the current
`.ai-dev/tasking.md` or the configured authorized rail, durable
repository/project state, and relevant capability skills. It must not depend on
conversation memory from a prior executor.
