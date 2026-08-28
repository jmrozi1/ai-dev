# Dory-Wrangler Issue #10 — Add context compaction, handoffs, and agent rotation

> Transport snapshot only. Canonical source: https://github.com/jmrozi1/dory-wrangler/issues/10
>
> Target repository: `~/dory-wrangler`. This file is not an AI Dev ticket or authorization to modify AI Dev.
>
> A human instruction must select the checkpoint to execute.

## Checkpoints

1. Define the durable packet required to start any agent role without hidden conversational history.
2. Preserve decisions, requirements, scope, evidence references, and unresolved questions across packet revisions.
3. Add bounded context selection and compaction for oversized task histories.
4. Rotate or replace an agent by launching a fresh session from the durable packet.
5. Detect and refuse incomplete or unprovable handoffs.
6. Prove continuation across compaction, replacement, restart, and disconnected-session scenarios.

## Acceptance Criteria

- No workflow depends on a persistent agent conversation.
- A fresh eligible agent can continue from durable state alone.
- Compaction preserves all active requirements, decisions, constraints, and unresolved items.
- Original raw outputs remain available as evidence even when omitted from the active packet.
- Packet provenance identifies the canonical records from which it was built.
- An incomplete handoff blocks rather than fabricating missing state.
- Rotation and replacement do not repeat completed work or skip required work.
- Tests include context overflow, repeated compaction, stale packets, and failed replacement.

## Full Description

Make finite context and unreliable sessions normal lifecycle events. This ticket ensures that agent memory is always reconstructible, allowing the harness to compact, rotate, or replace disposable sessions without appointing a persistent orchestrator.
