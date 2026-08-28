# Dory-Wrangler Issue #10 — Add context compaction, handoffs, and agent rotation

> Transport snapshot only. Canonical source: https://github.com/jmrozi1/dory-wrangler/issues/10
>
> Target repository: `~/dory-wrangler`. This file is not an AI Dev ticket or authorization to modify AI Dev.
>
> A human instruction must select the checkpoint to execute.

## Checkpoints

1. Define the durable packet required to start any agent role or continue a logical Dory conversation without hidden conversational history.
2. Preserve accepted requirements, decisions, scope, evidence references, unresolved questions, and the recent conversational context needed for a coherent next response.
3. Add bounded context selection and compaction for oversized task and conversation histories.
4. Rotate or replace an agent by launching a fresh session from the durable packet while preserving the same logical thread or task.
5. Detect and refuse incomplete or unprovable handoffs.
6. Prove continuation across compaction, replacement, restart, and disconnected-session scenarios.

## Acceptance Criteria

- No workflow depends on a persistent agent conversation.
- The user can continue the same logical Dory conversation after the underlying agent or provider session is replaced.
- Reusing a live provider session is an optional efficiency, not a source of authority.
- A fresh eligible agent can continue from durable state alone.
- Compaction preserves all active requirements, decisions, constraints, unresolved items, and enough conversational context to respond coherently.
- Raw messages and outputs remain available as evidence even when omitted from the active packet; accepted intent remains separately authoritative.
- Packet provenance identifies the canonical records from which it was built.
- An incomplete handoff blocks rather than fabricating missing state.
- Rotation and replacement do not repeat completed work or skip required work.
- Tests include conversation and task context overflow, repeated compaction, stale packets, and failed replacement.

## Full Description

Make finite context and unreliable sessions normal lifecycle events. This ticket ensures that agent memory and the user-facing Dory conversation are reconstructible, allowing the harness to compact, rotate, or replace disposable sessions without appointing a persistent orchestrator.

Conversation should feel continuous to the user while remaining operationally independent of any one agent process.
