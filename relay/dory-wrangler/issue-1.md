# Dory-Wrangler Issue #1 — Define the durable state and lifecycle contract

> Transport snapshot only. Canonical source: https://github.com/jmrozi1/dory-wrangler/issues/1
>
> Target repository: `~/dory-wrangler`. This file is not an AI Dev ticket or authorization to modify AI Dev.
>
> A human instruction must select the checkpoint to execute.

## Checkpoints

1. Inventory and freeze the currently proven web-to-agent bridge and user-service assumptions.
2. Define versioned durable schemas and paths for intent, tasks, agent runs, decisions, results, and failures.
3. Define valid lifecycle states, transitions, ownership, and the authority assigned to the harness, agents, and human.
4. Define atomic-write, identity, concurrency, and recovery invariants.
5. Prove the contract with representative valid and invalid fixtures and complete a final consistency audit.

## Acceptance Criteria

- Durable files, not process memory or an agent conversation, are canonical.
- The contract requires no persistent orchestrator.
- Every state and transition has an explicit owner and preconditions.
- Identifiers connect intent, tasks, runs, decisions, and results without relying on filenames or timestamps alone.
- Writes cannot expose partially updated canonical state.
- Unknown versions, malformed records, and unauthorized transitions fail closed.
- Fixtures cover normal completion, human waiting, failure, retry, replacement, and restart recovery.
- The existing bridge behavior remains usable throughout this ticket.

## Full Description

Dory-Wrangler needs a stable protocol before the harness and UI can safely coordinate work. This ticket defines the durable state model that every later component will consume.

The harness receives broad lifecycle authority but no authority to invent product meaning. Disposable agents may propose work and decisions, while only valid, policy-authorized transitions become canonical. The design must preserve the currently working bridge and session-bound operating model rather than rebuilding them.
