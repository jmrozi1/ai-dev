---
name: flow
description: Execute and validate AI Dev Flow lifecycle commands against the shared runtime.
---

# Copilot Flow

Use this skill for exact repository-facing operation of the shared Flow runtime.
Flow owns deterministic lifecycle state and Git safety; this skill supplies the
execution procedure and evidence discipline for bounded tasks.

## Contract

- Read the active tasking rail and verify `flow-status -v` before consequential
  work or lifecycle commands.
- Confirm the intended workflow identity, current branch, main/scratch relation,
  worktree cleanliness, checkpoint state, and blocked-workflow state.
- Use the installed direct launchers (`flow-start`, `flow-status`, `flow-diff`,
  `flow-commit`, `flow-promote`, `flow-complete`, and related commands) exactly
  as documented; do not route around the shared runtime with bespoke Git flows.
- Maintain checkpoint and task-rail state as current evidence, not as a
  transcript or execution diary.
- Before promotion, perform the required review-gate and repository safety
  checks. For tracked upstreams, preserve fetch-first preflight, ordinary push,
  durable pending state, safe retry, and completion synchronization rules.
- Validate command results with focused tests, status/diff output, and explicit
  evidence. Report failures, blockers, and recovery state precisely.
- Stop and escalate when preconditions fail, state is ambiguous, the command
  would mutate outside the task, or recovery would require unapproved merge,
  rebase, force-push, or reconciliation.

## Command Sequencing

Use `flow-status -v` first. Use `flow-diff` or `flow-diff --git` to inspect
scope, `flow-commit` for bounded checkpoints, and the lifecycle commands only
when the active task authorizes them. Do not run `flow-promote` or
`flow-complete` merely because implementation appears finished.

After a command, verify the resulting workflow state, branch relation,
worktree, and any durable synchronization evidence. Preserve a pending
promotion as recoverable state and retry only through the shared Flow command.

## Runtime Ownership

The deterministic runtime is implemented once in `ai_dev_flow`, with one set of
workflow-state files and one set of executable Flow launchers under this
Copilot-owned package. ChatGPT receives lifecycle interpretation guidance but no
copy of these launchers. Work receives no Flow package in this checkpoint.
