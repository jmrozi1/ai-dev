---
name: flow
description: Interpret AI Dev Flow lifecycle state and decide valid transitions and escalations.
---

# ChatGPT Flow

Use this skill for lifecycle meaning and decision-making around the shared Flow
runtime. Flow commands and workflow state remain deterministic repository
runtime owned by AI Dev; routine command mechanics belong to Copilot.

## Contract

- Understand the active issue or patch workflow, checkpoint, branch relation,
  clean/dirty state, blocked workflows, and pending synchronization state.
- Interpret `flow-status -v`, `flow-diff`, review-gate, and promotion-sync
  evidence as decision surfaces.
- Decide whether the next valid transition is start, checkpoint, promote,
  complete, reset, block, resume, or escalation, based on the active intent and
  Flow safety rules.
- Preserve orchestrator authority over lifecycle intent, scope, promotion,
  completion, and reconciliation decisions.
- Delegate routine command execution and repository inspection to Copilot when
  appropriate, with explicit scope and expected evidence.
- Escalate when state is blocked, contradictory, stale, unsafe, or requires a
  product, scope, permission, or reconciliation decision.

## Decision Boundaries

- Do not infer that a clean worktree or passing tests authorizes promotion.
- Do not treat a pending remote synchronization as completion.
- Do not authorize merge, rebase, force-push, or other reconciliation unless
  the governing workflow explicitly permits it; Flow normally refuses those
  paths.
- Do not replace the deterministic Flow runtime with bespoke Git procedure.

## Command and Report Use

ChatGPT may request read-only status or diff evidence when needed, but should
prefer compact reports that expose workflow identity, checkpoint, branch
relation, working-tree state, blocked/pending state, and the required decision.
It should avoid carrying Copilot-level launcher and shell procedure unless
reviewing an execution failure.

The shared runtime remains the source of truth for state transitions and
command results. This skill supplies lifecycle judgment, not a second command
implementation.
