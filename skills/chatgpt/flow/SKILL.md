---
name: flow
description: Interpret AI Dev Flow lifecycle state and decide valid transitions and escalations.
---

# ChatGPT Flow

Use this skill for lifecycle meaning and decision-making around the shared Flow
runtime. Flow commands and workflow state remain deterministic repository
runtime owned by AI Dev; routine command mechanics belong to the executor
audience. ChatGPT coordinates a named Claude or Coxswain orchestrator; that
orchestrator owns repository execution and any existing executor sessions.
ChatGPT never directly tasks executors.

## Contract

- Understand the active issue or patch workflow, checkpoint, branch relation,
  clean/dirty state, blocked workflows, and pending synchronization state.
- Interpret `flow-status -v`, `flow-diff`, review-gate, and promotion-sync
  evidence as decision surfaces.
- Delegate routine valid transitions within the approved mandate to the receiving
  orchestrator. Decide lifecycle intent when it is reserved to ChatGPT or escalated
  beyond that mandate, using active intent and Flow safety rules.
- Preserve the named owner's authority over promotion, completion and
  reconciliation; do not introduce a second checkpoint gate in ChatGPT.
- Route repository inspection and lifecycle outcomes to the Claude/Coxswain
  orchestrator, which owns command execution and executor coordination.
- Escalate when state is blocked, contradictory, stale, unsafe, or requires a
  product, scope, permission, or reconciliation decision.

## Decision Boundaries

- Do not infer that a clean worktree or passing tests authorizes promotion.
- Do not treat a pending remote synchronization as completion.
- Do not authorize merge, rebase, force-push, or other reconciliation unless
  the governing workflow explicitly permits it; Flow normally refuses those
  paths.
- Do not replace the deterministic Flow runtime with bespoke Git procedure.
- Do not treat the numeric Flow `checkpoint` as the authoritative named ticket
  roadmap index. Named ticket checkpoints remain the canonical implementation
  roadmap, while numeric Flow checkpoints are execution state.
- Completing a named checkpoint is the normal boundary for creating a Flow
  checkpoint commit and running checkpoint review; a review fix or retry may
  create extra Flow checkpoint commits without advancing the named roadmap.

## Command and Report Use

Understand `/status` semantics for orchestration, but delegate the interaction
to the receiving orchestrator. Claude exposes `ai-dev status`, which renders
the active ticket's named roadmap progress and may return Flow diagnostics when
they require a decision, recovery, or escalation. Flow's
numeric `checkpoint` is never the roadmap index. Avoid carrying executor-level
launcher and shell procedure unless reviewing such an execution failure.

The shared runtime remains the source of truth for state transitions and
command results. This skill supplies lifecycle judgment, not a second command
implementation.

## ChatGPT Interaction

When ChatGPT intentionally activates this skill, begin the user-facing response
with:

`Skill: flow`

This is an announcement only. Do not add a reasoning recommendation or a
proceed gate for routine lifecycle interpretation.
