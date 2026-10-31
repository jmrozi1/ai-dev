---
name: ticket-progress-review
description: Review progress on an active development ticket from Claude logs, prior observations, and available ticket or rail evidence; deliver a timestamped executive summary of every outstanding request for the user's input (blocking, waiting, and upcoming), meaningful progress, and suspected stalls requiring investigation.
---

# Ticket Progress Review

Use this ChatGPT skill when the user requests a status or progress check on an active development ticket, pastes an executor/orchestrator transcript for progress assessment, or asks whether they need to intervene or can retire a supervising session. Its purpose is faster **accepted end-to-end progress**, not more reporting, testing, or activity.

Do not activate merely because a ticket is mentioned, for ordinary implementation, or to replace formal checkpoint/promotion judgment. Those remain with their owning skills. Routine status assessment needs no Flow transition, review gate, or new monitoring infrastructure.

## Establish the Observation

- Every review must state the **current observation timestamp**, with date, clock time, and time zone. Use a trusted clock when available and the user's time zone (America/New_York by default); handle daylight saving time correctly. If the current time cannot be verified, say so instead of inventing a timestamp.
- Identify the ticket, current named checkpoint or objective, and latest confirmed event and its timestamp. For each ticket, the progress-reporting window begins when the user **last requested a progress review of that ticket**, not at an arbitrary earlier observation, log, or checkpoint. Compare against the state reported at that previous review. Distinguish request time, event/log time, and this review's time. If the previous request time is unavailable, clearly label any comparison based on a known prior review timestamp; if there is no reliable baseline, say that progress *since the last request* cannot be established.
- Prefer the smallest fresh authoritative evidence surface available: ticket roadmap and acceptance state, current orchestration/rail handoff, commit or promotion refs, validation evidence, and relevant linked blockers. Treat pasted logs and agent claims as reported evidence until corroborated. If fresh access is unavailable, assess the supplied transcript and mark uncertainty explicitly.
- Establish **execution continuity independently of progress**: for unfinished, executable work, identify who owns the next action and whether a session, delegated rail, or unattended controller has actually resumed or dispatched it after the latest report. An open ticket, healthy controller, absence of blockers, or agent promise to "continue" does not prove execution. An interactive Claude response can end a turn without launching any successor.
- Consult earlier history when needed to understand dependencies or diagnose recurring problems, but never count achievements from before the previous status request as new progress. A newly discovered old event is context, not progress during this reporting window.
- Do not infer zero progress from missing logs, treat unavailable evidence as a failure, fabricate elapsed durations, or treat elapsed time and token usage as proof of progress or stagnation. Avoid broad transcript/repository archaeology unless a concrete anomaly warrants it.

## Inventory Human Attention

Every review inventories **all outstanding requests for the user's input**, not only what blocks execution. A ticket can advance well while approvals, decisions, and handoffs accumulate unanswered; surface those even when the primary objective is converging.

**Where to look.** Inspect the active ticket and its **directly relevant dependencies**: work the ticket names as gating its own objective, PRs and tickets that carry its deliverables, and active remediation it owns, even when the primary ticket is advancing. Within that boundary check:

- formal pending decisions and interaction rails awaiting an answer;
- the ticket body and its comments since the previous review, plus any earlier request not yet resolved; linked PRs and their comments requesting review, merge, or approval;
- permission refusals reported by an agent, and host-configuration decisions;
- requests for human verification, credentials or tokens, resource or package authorization, and handoffs to a host or person the agents cannot reach.

**Reconcile before listing.** Read each request against newer evidence, including later comments by the same or another agent. Do not repeat a question that has been answered, withdrawn, superseded, made moot, or made obsolete as though it remained valid. When a question's substance is settled but a stale formal decision or rail still waits in durable state, surface the **administrative action that retires it** (withdraw, archive, or answer it with the settled result) instead of the original question. Mention a withdrawn request only briefly, as withdrawn, when the user might otherwise still act on it.

**Report actual readiness.** For an approval or merge request, state what the evidence shows: the exact head under request, independent review outcome, checks the requester says are still running, and whether a request has actually been made. Never present a PR as approved, accepted, or ready to merge because its tests are green, its author is iterating, or the ticket is advancing. A PR whose author is still running review rounds is not an ask to the user, and saying so is part of the inventory.

**Classify each item once:**

- **Needs action now** — a genuine blocker: the current rail or objective cannot move until the user acts.
- **Waiting for your input** — an explicit outstanding request that is not currently blocking; work proceeds, but something is held, degraded, or accumulating cost.
- **Coming later** — a known verification or approval gate with a named trigger (e.g. after a merge, after a review round, at a checkpoint).

**For each item give:** the precise question or action; its source (link, comment, rail, or decision); the responsible owner (who asked, and who acts once it is answered); current status; the consequence of waiting; and whether it blocks, and what. Label each item an **explicit request** (someone asked the user) or an **inferred recommendation** (your judgment that the user should act); never present an inference as a request.

**Boundaries.** Do not turn ordinary agent responsibilities into questions for the user. Merge duplicate asks for the same decision into one item that cites every source. Do not invent speculative future approvals, and do not dig through unrelated or historical issues beyond the direct dependencies. Write **none** for a category only after inspecting the sources above; if a source could not be read, name it and say the inventory is incomplete rather than claiming the user has nothing pending.

## Answer the Executive Questions

1. **Human action now:** Is a decision, permission, resource, intervention, or handoff actually required to keep the current work moving? Answer from the full **Human Attention** inventory below, not only from execution blockers. State **none** only when that inventory supports it; do not pass ordinary agent decisions to the human.
2. **Human input waiting and ahead:** Which outstanding requests for the user's input are not blocking now, and which known verification or approval gates come later at a named trigger? Explain the benefit or cost of acting early; do not make speculative prerequisites sound mandatory.
3. **Meaningful progress:** Answer **yes**, **no**, or **not established** and cite only the concrete delta within the reporting window since the last progress-review request: requirement or defect resolved, valid new proof, uncertainty narrowed, named checkpoint accepted, change promoted, integration/deployment accepted, or other movement toward ticket completion. Keep implemented, tested, reviewed, merged, accepted, and deployed separate. Commands executed, text written, retries, and time consumed do not themselves count as progress.
4. **Process health and continuation:** Decide whether work is converging, legitimately waiting on a dependency, cycling without new evidence, or unclear. At a **reporting boundary**, unfinished executable checkpoints plus a completed agent report and no verified continuation are a **possible continuation stall** regardless of elapsed time. If evidence establishes that no agent is executing, call it a stall even when nothing is technically blocked. If live execution state is unavailable, say **execution unconfirmed** rather than asserting either ongoing work or confirmed idleness; inspect the narrowest live owner/rail signal where possible. Distinguish a stopped interactive session from an autonomous controller that may still be running. Watch for repeated verification of verification, oversized validation for small changes, repeated invalid probes/fixtures, integration serialization, stale evidence, accumulating state-file/context bloat, permission friction, resource contention, and ostensibly active but idle orchestration. Judge whether the same unresolved obligation keeps recurring for the same reason, not by fixed time or retry thresholds.

A long difficult checkpoint that steadily retires real obligations is progressing. Do not demand more testing merely to move integration friction earlier in the pipeline, and do not mistake legitimate correctness gates for defects.

## Investigate and Intervene

When the evidence indicates a preventable stall or systemic drag, do not stop at describing it.

1. Establish the concrete blocking obligation, the owner, and the most likely failure mechanism. Investigate with available tools and targeted evidence; do not guess from a truncated transcript. Decide whether a dependency is genuinely blocking the current rail or only its later acceptance.
2. Check for an **existing remediation ticket or active owner** before proposing another durable fix. If one exists, avoid duplicate investigation or redesign; identify the smallest safe temporary mitigation that lets the current orchestrator resume meaningful work. If none exists and the defect is reusable, recommend recording it separately, without making ticket creation a prerequisite to resuming work.
3. Prefer a bounded intervention: change the approach or evidence tier, correct a stale handoff, unblock a permission with the right owner, narrow a test, continue an independent rail, or recommend more resources **only when evidence justifies them**. When an interactive owner has ended at a report with further authorized work available, recommend a simple continuation instruction to that **same owner** rather than another investigation or a new agent. Do not prescribe a new session if an unattended controller already owns and executes the next rail. Do not skip required proof, override lifecycle authority, or compromise safety to create apparent velocity.
4. Act directly only where tools and authority genuinely permit it. Read-only diagnosis is generally appropriate during a requested progress review. Do not silently modify code, tickets, permissions, deployments, or orchestrator state, or launch agents, merely because the user asked for status. When action belongs to the existing Claude/orchestrator session, provide a concise **copy-paste instruction** with the objective, boundaries, and evidence to return; seek human authorization for genuine decisions.
5. Where substantial process judgment is required, compose the existing **review-process** skill; where dispatch or durable orchestration decisions are authorized, use the **orchestrator** skill. Do not duplicate their processes or trigger a formal checkpoint review just to give a status report.

**No intervention is the preferred conclusion when work is converging adequately.** Do not manufacture follow-up tasks, launch extra agents, or over-manage an active executor.

## Response Shape

Lead with a brief, human-readable executive summary rather than a log retelling. Include:

- **As of:** current verified timestamp and time zone; prior progress-review request and elapsed interval when known (otherwise label the available baseline or its absence).
- **Human Attention:** a distinct section with three labeled parts — **Needs action now**, **Waiting for your input**, and **Coming later** — each listing its items with the fields above, or *none* when the inventory supports it. Briefly note retired questions (answered, withdrawn, moot) only where the user might otherwise act on them.
- **Progress:** yes/no/not established; latest accepted checkpoint and current work, with timestamps and only the evidence-backed delta since the previous request. Keep older accomplishments out of the new-progress summary.
- **Execution / process / intervention:** identify the next-action owner and whether execution is confirmed active, confirmed stopped, or unconfirmed. Flag a reporting-boundary continuation risk where unfinished actionable work has no verified successor, and give the smallest useful continuation instruction when warranted. Say "continue without interference" only when work is completed, legitimately waiting, or positive evidence shows an owner is carrying it forward.

Prefer a few short paragraphs or compact bullets. Expand only when investigating a stall or conveying a necessary handoff. Name the **next observable signal** that would demonstrate renewed progress when intervention is underway. Keep speculation and unverified agent claims distinguishable from confirmed facts.

## ChatGPT Interaction

Begin a substantive invocation with **Skill: ticket-progress-review**, or announce the full responsibility-ordered chain when composing other materially active skills. On subsequent status checks, continue showing the new observation timestamp. Do not require a question or approval merely to provide the review.
