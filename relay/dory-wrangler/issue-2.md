# Dory-Wrangler Issue #2 — Build the polling harness and idempotent scheduler

> Transport snapshot only. Canonical source: https://github.com/jmrozi1/dory-wrangler/issues/2
>
> Target repository: `~/dory-wrangler`. This file is not an AI Dev ticket or authorization to modify AI Dev.
>
> A human instruction must select the checkpoint to execute.

## Checkpoints

1. Implement read-only discovery of actionable durable records.
2. Add exclusive claiming or leasing for one eligible transition.
3. Add deterministic scheduling from validated state to one authorized next action.
4. Prevent duplicate launches across repeated polls and concurrent harness activity.
5. Reconstruct scheduler state after a service restart.
6. Exercise the scheduler against normal, malformed, stale, and conflicting state.

## Acceptance Criteria

- The harness can run continuously without requiring an AI session.
- Repeated polling is idempotent and cannot launch the same work twice.
- Only transitions authorized by the durable contract are scheduled.
- Claims have explicit ownership and cannot be silently stolen.
- Stale claims are handled through a defined recovery path.
- Unsupported or contradictory state blocks safely and produces actionable evidence.
- Restarting the service does not lose canonical work or fabricate progress.
- Tests prove duplicate prevention under concurrent polling.

## Full Description

Implement the deterministic core that watches canonical state and decides which already-authorized mechanical action is eligible next. The harness may launch, stop, retry, replace, or escalate according to policy, but it must not infer requirements or substitute its judgment for an executive, executor, reviewer, or human decision.
