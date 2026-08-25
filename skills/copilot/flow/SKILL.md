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
| `work a second ticket at the same time` | `scripts/flow-workspace add <id>` |
| `adopt this worktree for ticket <id>` | `scripts/flow-workspace adopt <id>` |
| `what workspaces exist?` | `scripts/flow-workspace list` |
| `my base is stale` | `scripts/flow-workspace refresh` |
| `remove this workspace` | `scripts/flow-workspace remove [path]` |
| `clean up dead workspace claims` | `scripts/flow-workspace prune` |
| `a promotion lock is stuck` | `scripts/flow-workspace unlock [--force] <holder-path>` |

### Concurrent Workspaces

One repository may host several active tickets at once. Each workspace is a
linked Git worktree with its own ticket-specific scratch branch and its own
`.ai-dev` state, so `flow-status`, `flow-diff`, `flow-commit`, `flow-reset`,
`flow-abandon`, and `flow-complete` act only on the workspace they are run from.
An ordinary single-workspace repository needs none of these commands and behaves
exactly as before.

- `scripts/flow-workspace add <id>` reserves the ticket claim before creating
  anything, then creates a sibling worktree and a deterministic branch, seeds
  that workspace's `.ai-dev`, and activates the ticket. If any step fails, it
  removes only what that invocation created; when cleanup itself fails it
  preserves the artifacts and reports them.
- `scripts/flow-workspace adopt <id>` claims and activates a worktree that
  already exists. It never resets the branch, index, or working tree, and infers
  the checkpoint from existing numbered commits.
- One ticket may be active as only one writable workspace. A second attempt is
  refused and names the owning workspace.
- A prerequisite handoff claims its own ticket in the workspace that starts it,
  and the suspended issue keeps its claim there, so neither ticket can be
  activated a second time elsewhere.
- `scripts/flow-workspace remove` refuses unless that workspace's workflow is
  inactive, its tree is clean, and its branch is not ahead of `main`.
- `scripts/flow-workspace prune` removes only claims whose worktree is gone or
  prunable. A claim held by a blocked workflow is live and is never pruned;
  recover it in its own workspace with `scripts/flow-resume <id>` followed by the
  appropriate lifecycle command.
- Claim ownership is the Git worktree identity. Reading another workspace's
  claim record never authorizes releasing it.
- The claim registry, not workflow state, is the authority on which ticket a
  worktree owns. When the two disagree, every ticket command and the
  control-plane rail lookup stop and name the owning workspace instead of
  choosing a side. Listing, pruning, unlocking, and removal keep working so the
  association can be repaired.
- A workspace never inherits a pinned control-plane ticket from the workspace
  that created it; it resolves the rail of the ticket it actually owns.
- The ticket catalogue is repository-level state, not workspace state. A `local`
  ticket store is read from the workspace that holds one, and otherwise from the
  primary worktree, so every concurrent workspace resolves its own ticket from
  one shared catalogue instead of a private copy.

### Promotion Serialization And Stale Bases

- Promotion is serialized repository-wide by a shared promotion lock. Contention
  fails closed and names the holding ticket and workspace; it never waits.
- Every promotion precondition is re-proved while the lock is held, so a `main`
  that advances after an early check is still caught before any mutation.
- Staleness is measured only against `main`. Another workspace's scratch branch
  advancing does not make this workspace stale.
- A stale base is refused without changing branches, index, working tree,
  workflow state, review evidence, or synchronization state. The refusal reports
  the ahead/behind counts and names the promoting workspace when that is known.
- `scripts/flow-workspace refresh` is the supported recovery. It merges the
  current `main` into this workspace's scratch branch under the promotion lock
  and records a non-numeric merge commit, so checkpoint numbering is unchanged.
  It never rebases, force-updates, resets, or modifies `main`.
- A workspace that is only behind `main` has nothing of its own to reconcile, so
  refresh fast-forwards it and records no commit. That keeps it completable; a
  merge there would leave it permanently ahead of `main` by an empty commit.
- Refresh clears promotion-review and review-baseline evidence bound to the old
  base. Review must be earned again before promoting.
- A conflicting refresh leaves the ordinary Git merge in progress for explicit
  resolution and keeps the workflow state, claim, and checkpoint intact. Resolve
  and commit it, or run `git merge --abort`. Flow never resolves or aborts it.
- `scripts/flow-workspace unlock <holder-path>` releases an abandoned promotion
  lock. Removal is automatic only for a same-host process proven absent;
  `--force` is required whenever liveness cannot be established.


### Prerequisite Handoff

- Use `scripts/flow-start <issue>` for independent work that starts from `main`.
- Use `scripts/flow-start <B> --prerequisite-for <A>` only when A is the active
  issue and B must inherit A's current clean `scratch` tree. The handoff
  requires no active Git operation, an exact active-A match, and a supported
  non-nested relationship.
- A becomes open/blocked and keeps its historical checkpoint ownership; B is
  active at checkpoint 0 and its diff/checkpoints begin at the inherited base.
- Promoting B intentionally publishes the complete physical A+B tree to `main`,
  including A's partial work. Completing B closes only B. Resuming A restores
  its prior checkpoint progression but starts a new empty scope from the
  promoted canonical commit; its next checkpoint is N+1.
- Managed refs and relationship metadata are internal Flow state. Users must not
  edit workflow JSON or manufacture branches, refs, or stashes. Patch adoption
  remains a distinct `flow-patch --adopt` workflow.
- After B is promoted and completed, a resumed A with no new active-scope work
  may complete directly at its recorded promoted commit; new A work still
  requires the normal checkpoint, review, promotion, and synchronization path.

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

`scripts/flow-report` prints the canonical read-only renderer output. It selects
the immediately preceding eligible completed Copilot turn and excludes its own
in-progress request; it does not create a second synthesized response or mutate
local state.

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
