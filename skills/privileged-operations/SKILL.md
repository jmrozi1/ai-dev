---
name: privileged-operations
description: Run a single operator-authorized host task requiring sudo or administrator privileges through a manually launched, short-lived Claude executor. Bound the change, use native permission approval, verify it, and end the session; never elevate a resident orchestrator.
---

# Privileged Operations

Use when a real host operation needs sudo, root-owned file changes, service lifecycle control, protected backup/restore, or other administrator authority, and an AI assistant will help perform it. The default model is **human-dispatched, short-lived privileged execution**, not unattended elevated orchestration.

Do not activate for normal source development, GitHub promotion, read-only diagnostics that need no elevated access, or an ordinary Claude/Coxswain executor whose authorized work is unprivileged. If an existing narrowly scoped privileged helper is already installed and sufficient, prefer it; this skill is not a mandate to launch an administrator session.

## Separate Roles and Privileges

- **Coordinator (ChatGPT or current project owner):** decide the already-authorized outcome and prepare one bounded, self-contained assignment. Retain scope, production/release decisions, and acceptance judgment. Do not treat the privileged session as a new orchestrator or release owner.
- **Human operator:** explicitly launches a **fresh, interactive Claude executor** under an existing administrator login with sudo access. Keep an independent operator recovery shell when changing services or authorization. Do not launch Claude itself through sudo, run it as root, put the resident controller service account into sudoers, or use an unattended privileged Claude/controller process.
- **Privileged Claude:** executes only the named assignment. No subagents, delegation, autonomous scheduling, task queue, additional tickets, unrelated fixes, or permission-system workarounds. Stop after the one result or a genuine safety/authorization refusal.
- Use the provider's **interactive, ask-before-privileged-action permission mode**. Normal sudo and Claude Code permission prompts are separate: host sudo capability does not override a tool permission denial. Never use a permission-bypass flag or secretly change allow/deny settings. Passwords and tokens are entered only through trusted interactive host prompts, never in chat, tasking text, repository files, or logs.

Launching under an administrator account is **not a sandbox**. A bounded prompt limits intended behavior, not the operating-system authority available to that account. Keep approval at each materially privileged action; batch routine evidence gathering and adjacent steps where the provider safely supports it.

## Bound the Assignment Before Dispatch

Supply one task with just the information needed to act safely:

- **Outcome and target:** host, exact service/path/environment and approved candidate or expected resulting state, when relevant.
- **Allowed work:** the smallest supported procedure and side effects needed to reach that outcome. Reuse the existing runbook, backup tool, deploy helper, or service mechanism before creating new machinery.
- **Forbidden territory:** unrelated units, environments, projects, credentials, Git branches and running work. State interruption limits and whether a controlled stop is already authorized.
- **Verification and recovery:** observations that distinguish success from a command returning zero; the rollback or stop condition for stateful changes; where to record concise evidence.
- **End:** report one outcome and close this Claude session. Do not ask it to pick the next task.

Do not turn a single service restart, approved deployment, protected-file edit, or diagnostic into a requirements workshop, a new Flow rail, a standing privileged service, or a root-owned framework. A larger job should be decomposed **before** launching the privileged executor, with separate sessions for truly distinct operations.

## Execute in One Short Session

1. **Preflight once.** Confirm the actual OS identity, target host/service, current state, approved target, any competing host-action owner, and the existing supported procedure. Read only what is material. Stop if the target, authorization, rollback route, or singleton ownership differs materially from the assignment.
2. **One compact execution plan.** State the intended change, expected interruption, critical rollback and checks. If the operator has already approved the task and its normal effects, do not request a duplicate conversational approval. Let the native permission control request approval for the actual privileged operations.
3. **Execute the smallest authorized sequence.** Keep exact service and paths. Never run untrusted project or downloaded code as root merely because it is in an approved revision. Do not substitute a broad root shell, arbitrary privileged script, force operation, or repeated permission experiment for a rejected command. If the provider denies an action, report the exact gate and use its supported interactive approval flow; do not route around the denial.
4. **Verify the outcome.** Read back actual installed/active state and the task-specific behavior, not merely process existence or a successful command exit. For stateful work, preserve a consistent backup/recovery path; if the change fails, restore or safely stop as supported, record what remains unresolved, and never claim an ambiguous operation succeeded. Verify unrelated protected services were not changed when the task depends on that isolation.
5. **Handoff once and stop.** Report completed, blocked or failed; exact target/revision; decisive checks; rollback or recovery state; and at most one specific human action if needed. Write a concise ticket comment only if the assignment authorized it. End the session; no invitation to continue, extra investigations, or next assignment.

A real unexpected safety condition warrants an immediate stop and concise escalation. Routine commands, already-agreed implementation details, and expected permission prompts do not warrant repeated chat questions. Minimize back-and-forth without weakening approval for privileged changes.

## Coordinator's Copy-Paste Assignment

Adapt this compact template rather than prescribing an entire procedure in chat:

> Act as a **short-lived privileged Claude executor, not an orchestrator**.
> On [host], perform only [one approved operation] against [exact target/revision].
> Use [existing supported runbook/helper]. Do not touch [forbidden services/paths].
> Verify [observable success], protect [backup/rollback or interruption constraint], and record [minimal evidence destination].
> Use interactive approvals for privileged commands. Stop on a material safety or authorization mismatch; do not improvise a bypass.
> After this single assignment, report success/failure and evidence, then **end the session**. Do not take further work.

Do not claim that preparing this prompt or creating a GitHub issue launched Claude or performed a host operation. The human dispatches the privileged executor. After the handoff, normal unprivileged owners handle downstream review, source work and release acceptance.

## Activation Examples and Boundaries

- **Yes:** a one-time, operator-approved production controller deployment requiring sudo; install a validated protected config; restore-verify a root-owned backup; restart one named unit and prove recovery.
- **No:** implement and review a GitHub PR; run normal tests as a non-root executor; continuously monitor a controller; make a resident orchestrator privileged; design a general deployment engine when a bounded host operation was requested.

## ChatGPT Interaction

When ChatGPT uses this skill, announce **Skill: privileged-operations** or a responsibility-ordered chain including it. Deliver a short runnable task instruction and say what operator dispatch is actually required. For an in-progress privileged session, evaluate its narrow evidence and decide whether it has reached its stop boundary; do not expand its remit after a successful handoff.
