---
name: flow
description: Use for Flow lifecycle execution and safety verification; load when work involves flow-* commands, checkpoints, branch/repository safety, flow-diff modes, or ticket workflow transitions.
---

# Flow Skill

## Intent

Use this skill when an agent is executing or validating repository workflows with the retained Flow runtime.

Flow is a deterministic workflow runtime for issue/patch lifecycle control and safe Git state transitions. It is not an AI planning, summarize, review-packaging, or routing framework.

## What Flow Owns

Flow owns:

- Active workflow state in `.ai-dev/workflow.json`.
- Blocked workflow state in `.ai-dev/blocked-workflows.json`.
- Lifecycle transitions for issue and patch workflows.
- Checkpoint progression on `scratch` and promotion to `main`.
- Diff inspection and ephemeral review-baseline management via `flow-diff` modes.
- Ticket-provider operations for bound workflow tickets and explicit ticket commands.
- Safety checks around branch relation, clean/dirty state, and active Git operations.

Flow does not own:

- Runtime summarize/review/task-report generation.
- Runtime command routing/index frameworks.
- Repository output-file routing through `.ai-dev/config.json`.
- Deletion of legacy documentation artifacts by default.

## When To Load This Skill

Load this skill when work involves any of:

- Running Flow lifecycle commands.
- Verifying workflow safety before implementation.
- Interpreting checkpoint state and branch relation.
- Using `flow-diff` as the change-inspection surface.
- Diagnosing ticket lifecycle behavior in `start/block/resume/complete`.
- Verifying installer-managed Flow launcher availability.

Do not load this skill for:

- Designing new runtime frameworks or skill routers.
- Reintroducing retired runtime surfaces (`flow` dispatcher, `config/apply/update`, summarize/review runtimes).

## Authority Model (Orchestrator vs Executor)

- Orchestrator authority: decides intent, scope, and when to perform consequential transitions (start/commit/promote/reset/complete/block/resume).
- Executor authority: runs commands exactly, reports results, and fails closed on safety violations.
- Executor must not infer a different active workflow than current state; always verify workflow identity before implementation.

## Installed Command Surface (13 Commands)

Current fixed Flow executables are:

- `flow-start`
- `flow-patch`
- `flow-status`
- `flow-diff`
- `flow-commit`
- `flow-reset`
- `flow-promote`
- `flow-complete`
- `flow-block`
- `flow-resume`
- `flow-ticket-create`
- `flow-ticket-show`
- `flow-ticket-query`

## Issue and Patch Lifecycle

Issue lifecycle (high level):

1. `flow-start <issue-number>` binds workflow to a ticket and resets `scratch` from `main`.
2. Implement changes.
3. `flow-commit` creates numbered checkpoints on `scratch`.
4. `flow-promote "<message>"` squashes workflow changes onto `main` and realigns `scratch`.
5. `flow-complete` closes/completes the bound ticket and clears active workflow state.

Patch lifecycle (high level):

1. `flow-patch "<description>"` starts a patch workflow, or `flow-patch --adopt "<description>"` adopts existing `scratch` work.
2. Implement and checkpoint with `flow-commit` as needed.
3. Promote and complete the workflow similarly to issue flow.

Block/resume lifecycle:

- `flow-block "<reason>"` transitions an active issue workflow to blocked and releases the active slot.
- `flow-resume <ticket-number>` restores a blocked issue workflow as active.

## Diff Semantics

`flow-diff` performs repository inspection and does not mutate Git refs, commits, working tree, real index, or stash state.

Supported modes:

- `flow-diff`: read-only repository inspection. Shows baseline-relative uncommitted changes when baseline is valid; otherwise full current uncommitted delta.
- `flow-diff --refresh`: updates only Flow-owned ephemeral review-baseline metadata for the active workflow scope.
- `flow-diff --git`: read-only repository inspection of full current uncommitted delta, ignoring baseline.
- `flow-diff --all`: read-only repository inspection of full active-workflow delta since `main`, plus current uncommitted changes.

`flow-diff --refresh` must not mutate Git refs, commits, working tree, real index, or stash state.

## Checkpoint Semantics

- Checkpoint is durable workflow progress stored in workflow state and incremented by `flow-commit`.
- `flow-promote`, `flow-reset`, `flow-complete`, `flow-block`, and `flow-resume` enforce/restore checkpoint-safe states as part of transition safety.
- Review baseline is separate and ephemeral; it is not a checkpoint.

## Consequential vs Read-Only Commands

Consequential commands (mutate workflow state, Git state, or ticket state):

- `flow-start`
- `flow-patch`
- `flow-commit`
- `flow-reset`
- `flow-promote`
- `flow-complete`
- `flow-block`
- `flow-resume`
- `flow-ticket-create`

Read-only commands:

- `flow-status`
- `flow-diff` (including `--git` and `--all`)
- `flow-ticket-show`
- `flow-ticket-query`

Metadata-only mutation:

- `flow-diff --refresh` updates ephemeral Flow review-baseline metadata only; it is non-consequential for Git repository state.

## Safety Requirements

Before implementation or consequential commands:

1. Verify intended active workflow with `flow-status -v`.
2. Confirm branch context and relation (`main` vs `scratch`).
3. Confirm working-tree expectations (clean when command requires it).
4. Confirm no blocked-workflow or active-workflow conflicts.

Fail-closed behavior is expected: if safety preconditions are not met, stop and resolve state first.

## Verification Of Results

After command execution, verify by reading command output and state evidence:

- `flow-status -v` for active workflow, checkpoint, relation, and working-tree state.
- `flow-diff`/`flow-diff --git`/`flow-diff --all` for expected scope.
- `git status --short` when repository cleanliness is required by process.
- Ticket commands (`flow-ticket-show`, `flow-ticket-query`) when ticket state transitions matter.

## Installation and Repair Behavior (High Level)

Installer ownership is bootstrap-managed.

- `scripts/install.sh` installs or refreshes prefixed fixed launchers (default prefix `flow`).
- Installer tracks owned launchers and digests in persistent ownership metadata.
- Reconciliation is ownership-safe: managed stale launchers can be removed when ownership proof is valid.
- Divergent or unowned launcher files are preserved (fail-closed), not force-deleted by default.

This skill documents runtime usage and verification only. It does not move runtime implementation out of `ai_dev_flow`.
