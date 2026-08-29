# Dory-Wrangler Issue #4 — Implement the human-attention queue API

> Transport snapshot only. Canonical source: https://github.com/jmrozi1/dory-wrangler/issues/4
>
> Target repository: `~/dory-wrangler`. This file is not an AI Dev ticket or authorization to modify AI Dev.
>
> A human instruction must select the checkpoint to execute.

## Checkpoints

1. Derive attention items and state counts from canonical durable state.
2. Add oldest-first queue retrieval with Waiting, Running, and Disconnected filters.
3. Add retrieval of one decision explanation and its evidence/details.
4. Accept a free-form human response and apply the authorized state transition atomically.
5. Add refresh, validation, conflict, and stale-response handling.
6. Verify the API against representative queue and response scenarios.

## Acceptance Criteria

- The API never maintains a second canonical copy of workflow state.
- Waiting items are ordered oldest first.
- Minimal queue rows provide subject, project or ticket, and waiting time.
- A decision response is free-form text; the API does not reduce it to accept/reject buttons.
- A stale or duplicate response cannot advance state twice.
- Details expose evidence and output without duplicating the primary decision explanation.
- Submitting a valid response makes the resulting work eligible for harness continuation.
- Invalid and conflicting responses fail clearly without corrupting state.

## Full Description

Provide the backend contract for the Coxswain-style attention interface. The user should interact with decisions, not agent sessions, and every response must enter the same durable lifecycle consumed by the harness.
