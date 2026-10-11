---
name: ticket-progress-review
description: Review progress on an active development ticket from Claude logs, prior observations, and available ticket or rail evidence; deliver a timestamped executive summary of human actions, upcoming dependencies, meaningful progress, and suspected stalls requiring investigation.
---

# Ticket Progress Review

Use this ChatGPT skill when the user requests a status or progress check on an active development ticket, pastes an executor/orchestrator transcript for progress assessment, or asks whether they need to intervene or can retire a supervising session. Its purpose is faster **accepted end-to-end progress**, not more reporting, testing, or activity.

Do not activate merely because a ticket is mentioned, for ordinary implementation, or to replace formal checkpoint/promotion judgment. Those remain with their owning skills. Routine status assessment needs no Flow transition, review gate, or new monitoring infrastructure.

## Establish the Observation

- Every review must state the **current observation timestamp**, with date, clock time, and time zone. Use a trusted clock when available and the user's time zone (America/New_York by default); handle daylight saving time correctly. If the current time cannot be verified, say so instead of inventing a timestamp.
- Identify the ticket, current named checkpoint or objective, latest confirmed event and its timestamp, and the **previous observation** when available. Compare their times and evidence. Distinguish the time a log was generated, the time it was pasted, and the time this review was made.
- Prefer the smallest fresh authoritative evidence surface available: ticket roadmap and acceptance state, current orchestration/rail handoff, commit or promotion refs, validation evidence, and relevant linked blockers. Treat pasted logs and agent claims as reported evidence until corroborated. If fresh access is unavailable, assess the supplied transcript and mark uncertainty explicitly.
- Do not infer zero progress from missing logs, treat unavailable evidence as a failure, fabricate elapsed durations, or treat elapsed time and token usage as proof of progress or stagnation. Avoid broad transcript/repository archaeology unless a concrete anomaly warrants it.

## Answer the Executive Questions

1. **Human action now:** Is a decision, permission, resource, intervention, or handoff actually required to keep the current work moving? State **none** when appropriate. Identify the exact action and why it is blocking; do not pass ordinary agent decisions to the human.
2. **Human action ahead:** Surface known future human dependencies even if they are not yet blocking. Distinguish *prepare now*, *later at a named trigger/checkpoint*, and *no preparation needed*. Explain the benefit or cost of acting early; do not make speculative prerequisites sound mandatory.
3. **Meaningful progress:** Answer **yes**, **no**, or **not established** and cite the concrete delta since the last known observation: requirement or defect resolved, valid new proof, uncertainty narrowed, named checkpoint accepted, change promoted, integration/deployment accepted, or other movement toward ticket completion. Keep implemented, tested, reviewed, merged, accepted, and deployed separate. Commands executed, text written, retries, and time consumed do not themselves count as progress.
4. **Process health:** Decide whether work is converging, legitimately waiting on a dependency, cycling without new evidence, or unclear. Watch for repeated verification of verification, oversized validation for small changes, repeated invalid probes/fixtures, integration serialization, stale evidence, accumulating state-file/context bloat, permission friction, resource contention, and ostensibly active but idle orchestration. Judge whether the same unresolved obligation keeps recurring for the same reason, not by fixed time or retry thresholds.

A long difficult checkpoint that steadily retires real obligations is progressing. Do not demand more testing merely to move integration friction earlier in the pipeline, and do not mistake legitimate correctness gates for defects.

## Investigate and Intervene

When the evidence indicates a preventable stall or systemic drag, do not stop at describing it.

1. Establish the concrete blocking obligation, the owner, and the most likely failure mechanism. Investigate with available tools and targeted evidence; do not guess from a truncated transcript. Decide whether a dependency is genuinely blocking the current rail or only its later acceptance.
2. Check for an **existing remediation ticket or active owner** before proposing another durable fix. If one exists, avoid duplicate investigation or redesign; identify the smallest safe temporary mitigation that lets the current orchestrator resume meaningful work. If none exists and the defect is reusable, recommend recording it separately, without making ticket creation a prerequisite to resuming work.
3. Prefer a bounded intervention: change the approach or evidence tier, correct a stale handoff, unblock a permission with the right owner, narrow a test, continue an independent rail, or recommend more resources **only when evidence justifies them**. Do not skip required proof, override lifecycle authority, or compromise safety to create apparent velocity.
4. Act directly only where tools and authority genuinely permit it. Read-only diagnosis is generally appropriate during a requested progress review. Do not silently modify code, tickets, permissions, deployments, or orchestrator state, or launch agents, merely because the user asked for status. When action belongs to the existing Claude/orchestrator session, provide a concise **copy-paste instruction** with the objective, boundaries, and evidence to return; seek human authorization for genuine decisions.
5. Where substantial process judgment is required, compose the existing **review-process** skill; where dispatch or durable orchestration decisions are authorized, use the **orchestrator** skill. Do not duplicate their processes or trigger a formal checkpoint review just to give a status report.

**No intervention is the preferred conclusion when work is converging adequately.** Do not manufacture follow-up tasks, launch extra agents, or over-manage an active executor.

## Response Shape

Lead with a brief, human-readable executive summary rather than a log retelling. Include:

- **As of:** current verified timestamp and time zone; previous observation and elapsed interval when known.
- **You now:** exact required action, or none.
- **Ahead:** useful preparation, its urgency/trigger, or none.
- **Progress:** yes/no/not established; latest accepted checkpoint and current work, with timestamps and the evidence-backed delta since last observation.
- **Process / intervention:** health judgment and concrete next intervention only if warranted; otherwise say continue without interference.

Prefer a few short paragraphs or compact bullets. Expand only when investigating a stall or conveying a necessary handoff. Name the **next observable signal** that would demonstrate renewed progress when intervention is underway. Keep speculation and unverified agent claims distinguishable from confirmed facts.

## ChatGPT Interaction

Begin a substantive invocation with **Skill: ticket-progress-review**, or announce the full responsibility-ordered chain when composing other materially active skills. On subsequent status checks, continue showing the new observation timestamp. Do not require a question or approval merely to provide the review.
