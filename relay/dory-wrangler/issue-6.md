# Dory-Wrangler Issue #6 — Add conversational intent intake and readiness gating

> Transport snapshot only. Canonical source: https://github.com/jmrozi1/dory-wrangler/issues/6
>
> Target repository: `~/dory-wrangler`. This file is not an AI Dev ticket or authorization to modify AI Dev.
>
> A human instruction must select the checkpoint to execute.

## Checkpoints

1. Start a durable logical Dory conversation when the user submits an undeveloped request through the proactive console.
2. Launch a bounded disposable executive task with the Dory role, the raw request, and the current durable conversation and intent state.
3. Continue the same logical conversation across user messages, agent replacement, and service restart without requiring the same provider session.
4. Identify missing outcomes, ambiguity, scope, constraints, verification expectations, and material decisions; return useful conversational responses and waiting-human attention items as appropriate.
5. Reassess and refine accepted intent after each response without losing raw input or prior decisions.
6. Produce an executable task only when readiness criteria are satisfied and the current human or policy authorization permits launch.
7. Verify ready, ambiguous, contradictory, abandoned, resumed, compacted, and replaced-agent paths.

## Acceptance Criteria

- The user can begin with a rough natural-language prompt rather than an implementation-ready ticket.
- Raw intent is preserved separately from later interpretation and accepted requirements.
- The user experiences one continuing Dory conversation even when the underlying executive task is relaunched or rotated.
- Reusing an available model session is optional; losing it cannot lose accepted intent or prevent continuation.
- Poorly defined work cannot launch an executor.
- Readiness assessment and conversational refinement are performed by bounded disposable agent tasks, not a persistent orchestrator.
- Questions can be answered in the active console and remain available through the normal attention queue when the thread is waiting on the human.
- Prior decisions remain durable across reassessment, compaction, and agent replacement.
- Readiness alone does not grant execution authority; the harness starts implementation only when the defined authorization policy also permits it.
- Contradictory or materially expanded intent returns to the human rather than being guessed through.

## Full Description

Dory-Wrangler must replace the product-shaping role that an external ChatGPT conversation would otherwise perform. This ticket adds the multi-turn conversational loop that turns a rough request into accepted, executable requirements and then hands authorized work to the deterministic harness.

Dory is a persistent logical role and conversation, not a permanently running agent process. Disposable executive tasks perform semantic refinement from durable state, while the harness owns lifecycle mechanics and refuses execution until the intent is ready and authorized.
