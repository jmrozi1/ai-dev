# Dory-Wrangler Issue #6 — Add intent intake and readiness gating

> Transport snapshot only. Canonical source: https://github.com/jmrozi1/dory-wrangler/issues/6
>
> Target repository: `~/dory-wrangler`. This file is not an AI Dev ticket or authorization to modify AI Dev.
>
> A human instruction must select the checkpoint to execute.

## Checkpoints

1. Capture undeveloped user requests as durable intent without treating them as executable work.
2. Launch a disposable executive task to assess completeness, ambiguity, scope, and required decisions.
3. Convert unresolved questions into human-attention items.
4. Reassess intent after each response without losing prior decisions.
5. Produce an executable task only when readiness criteria are satisfied.
6. Verify ready, ambiguous, contradictory, and abandoned intent paths.

## Acceptance Criteria

- Raw intent is preserved separately from later interpretation.
- Poorly defined work cannot launch an executor.
- Readiness assessment is performed by a bounded disposable agent task, not a persistent orchestrator.
- Questions appear through the normal attention queue and accept free-form answers.
- Prior decisions remain durable across reassessment and agent replacement.
- The harness starts implementation only from an explicitly ready task.
- Contradictory or materially expanded intent returns to the human rather than being guessed through.

## Full Description

Dory-Wrangler should orchestrate the user as well as the agents by refusing to begin underdefined work. This ticket adds the shaping loop that turns captured intent into authorized work while keeping semantic judgment outside the deterministic harness.
