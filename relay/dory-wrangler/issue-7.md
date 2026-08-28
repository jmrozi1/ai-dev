# Dory-Wrangler Issue #7 — Define disposable executive, executor, and reviewer tasks

> Transport snapshot only. Canonical source: https://github.com/jmrozi1/dory-wrangler/issues/7
>
> Target repository: `~/dory-wrangler`. This file is not an AI Dev ticket or authorization to modify AI Dev.
>
> A human instruction must select the checkpoint to execute.

## Checkpoints

1. Define the common bounded-task input and output envelope.
2. Define executive task contracts for conversational intent shaping, decomposition, sequencing, and orchestration proposals.
3. Define the executor task contract for narrowly authorized implementation or investigation.
4. Define the independent reviewer task contract and freshness requirements.
5. Validate all role outputs before they can affect canonical state.
6. Exercise successful, incomplete, malformed, over-scoped, self-escalating, and replaced-session outputs.

## Acceptance Criteria

- Executive, executor, and reviewer are temporary assignments, not persistent identities.
- Every invocation receives a complete durable packet for its bounded responsibility.
- The user-facing Dory identity may span many executive invocations, but no individual process or provider session becomes authoritative.
- A conversational executive can return a user response, proposed intent refinements, unresolved questions, and readiness evidence without directly authorizing execution.
- Agents cannot directly launch or authorize other agents.
- Executive tasks may propose child work, parallelism, dependencies, and reviews in a structured form.
- Executors cannot expand scope or silently reinterpret requirements.
- Reviewers are independent of the work they judge where policy requires freshness.
- Malformed, unauthorized, or incomplete output fails closed and enters a defined remediation or escalation path.

## Full Description

Create the role contracts that let interchangeable short-memory agents participate safely. The harness owns lifecycle mechanics, while disposable executive tasks provide bounded semantic work: they can speak as Dory during intent shaping or produce orchestration proposals after work is authorized.

Logical conversational continuity comes from durable state supplied to each invocation, not from appointing a persistent orchestrator or trusting one model session indefinitely.
