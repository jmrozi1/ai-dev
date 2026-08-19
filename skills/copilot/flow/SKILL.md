---
name: flow
description: Report active-ticket and project status; execute natural-language lifecycle intents (start ticket, start patch, checkpoint this, close this out); manage exact repository-facing Flow lifecycle operations.
---

# Copilot Flow

Use this skill for exact repository-facing operation of the shared Flow runtime.
Flow owns deterministic lifecycle state and Git safety; this skill supplies the
execution procedure and evidence discipline for bounded tasks.

## Contract

- Read the active tasking rail and verify the skill-local `scripts/flow-status`
  helper with `-v` before consequential work or lifecycle commands.
- Confirm the intended workflow identity, current branch, main/scratch relation,
  worktree cleanliness, checkpoint state, and blocked-workflow state.
- Execute lifecycle operations through this installed package's `scripts/flow-*`
  helpers. Do not rely on `PATH`-installed launchers, and do not route around
  the shared runtime with bespoke Git flows.
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

Use `scripts/flow-status -v` first. Use `scripts/flow-diff` or
`scripts/flow-diff --git` to inspect scope, `scripts/flow-commit` for bounded
checkpoints, and the lifecycle commands only when the active task authorizes
them. Do not run `scripts/flow-promote` or `scripts/flow-complete` merely
because implementation appears finished.

## Intent Mapping

Map natural-language lifecycle requests to this package's local helpers:

| Intent | Helper |
| --- | --- |
| `start ticket <id>` | `scripts/flow-start <id>` |
| `start a patch` | `scripts/flow-patch "<description>"` |
| `checkpoint this` | `scripts/flow-commit` |
| `abandon this workflow` | `scripts/flow-abandon` |
| `abandon the current workflow` | `scripts/flow-abandon` |
| `cancel this local workflow` | `scripts/flow-abandon` |
| `stop tracking this issue locally` | `scripts/flow-abandon` |
| `clear the local workflow but leave the ticket alone` | `scripts/flow-abandon` |
| `reset this` | `scripts/flow-reset` |
| `block this` | `scripts/flow-block "<reason>"` |
| `resume ticket <id>` | `scripts/flow-resume <id>` |
| `close this out` | `scripts/flow-promote "<message>"`, then `scripts/flow-complete` when authorized |
| `/status` or `what's my status?` | `scripts/ticket-status` |
| `/status verbose` | `scripts/ticket-status verbose` |

## Lifecycle Distinctions

When the user asks to abandon, cancel, or clear the local workflow without changing the ticket, the executor must read the exact semantics before choosing a command:

- flow-reset = destructive execution reset = keeps the workflow/ticket binding active = resets scratch to main and may discard unpromoted workflow work
- flow-complete = provider/ticket completion = completes the workflow = may mutate/close/update the bound ticket through its provider
- flow-abandon = local-only abandonment = never mutates the bound ticket/provider = never resets branches or discards repository content = succeeds only when the repository is already clean and synchronized

Never use flow-reset as a substitute for abandon.
Never use flow-complete when the user explicitly wants the ticket left unchanged.
Never use __test-state-clear for production/local-abandon intent.

If flow-abandon refuses because scratch is ahead/behind/diverged, the tree is dirty, or Git has an active operation, report the exact blocker, preserve the workflow and repository state, and fail without mutation. Do not automatically run flow-reset, git reset, cleanup, commit, promote, or another destructive command just to make abandon succeed. The user or orchestrator should decide how to resolve that work first.

This is a local-only lifecycle action; do not broaden it into a generalized recovery or reset framework.

## Start-Ticket Authority

For `start ticket <id>`, do not broadly search the repository for ticket title,
requirements, acceptance criteria, or description before activation. Run the
package-local `scripts/flow-start <id>` helper so the configured ticket provider
activates the requested issue. After it succeeds, use the provider-fetched
active issue and `scripts/ticket-status verbose` as the sole current ticket
intent for title, checkpoints, acceptance criteria, and Full Description.

Do not consult or merge root `tickets.md`, `current-task.md`, historical task
artifacts, or other local catalogs as current ticket intent. A repository may
still use `.ai-dev/tickets/*.json` when its configured provider is explicitly
`local`; that local-provider data must not be confused with root historical
documentation. Historical files may be inspected only when the user explicitly
asks for historical context.

Named ticket checkpoints are the canonical implementation roadmap. The numeric
Flow `checkpoint` is deterministic execution state and is not the authoritative
roadmap index. Review fixes or retries may produce extra Flow checkpoint commits
without advancing the named roadmap.

After a command, verify the resulting workflow state, branch relation,
worktree, and any durable synchronization evidence. Preserve a pending
promotion as recoverable state and retry only through the shared Flow command.

## Diagnostic Status

`scripts/flow-status` remains a Copilot-only diagnostic and lifecycle-evidence
helper. Do not present its repository details as normal project progress.
Service `/status` through this package's `scripts/ticket-status` helper. It
delegates to the canonical ticket-status renderer, not `flow-status`, and does
not require a PATH-installed command. Surface Flow diagnostic output only when
it identifies a problem requiring user or orchestrator attention.

## Runtime Ownership

The deterministic lifecycle runtime is implemented once in `ai_dev_flow`; Flow
helpers route to `python -m ai_dev_flow.cli __ai_dev_flow_exec__ <command>`, and
the ticket-status helper routes to `python -m ai_dev_flow.ticket_status`.
The package includes POSIX executable helpers and PowerShell helpers for the
native Windows Copilot environment. PATH-installed Flow launchers and ticket
commands are retired; this package is the supported execution path. ChatGPT
receives lifecycle interpretation guidance but does not own the `/status`
interaction. Work receives no Flow package in this checkpoint.
