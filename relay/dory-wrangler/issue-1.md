# Dory-Wrangler Issue #1 — Define the durable state and lifecycle contract

> Transport snapshot only. Canonical source: https://github.com/jmrozi1/dory-wrangler/issues/1
>
> Target repository: `~/dory-wrangler`. This file is not an AI Dev ticket or authorization to modify AI Dev.
>
> A human instruction must select the checkpoint to execute.

## Checkpoints

1. Inventory and freeze the currently proven web-to-agent bridge and user-service assumptions.
2. Define versioned durable schemas and paths for raw intent, logical conversation threads and messages, accepted requirements and decisions, tasks, agent runs, results, and failures.
3. Define valid lifecycle states, transitions, ownership, and the authority assigned to the harness, agents, and human.
4. Define atomic-write, identity, concurrency, and recovery invariants.
5. Prove the contract with representative valid and invalid fixtures, including conversation continuation after restart, and complete a final consistency audit.

## Acceptance Criteria

- Durable files, not process memory or an agent conversation, are canonical.
- Raw user messages and agent replies remain durable evidence, while accepted requirements and decisions define the executable intent.
- A logical Dory conversation can continue after agent replacement or service restart without depending on a provider session identifier.
- Reusing an available provider session is permitted only as an optimization and does not make that session authoritative.
- The contract requires no persistent orchestrator.
- Every state and transition has an explicit owner and preconditions.
- Identifiers connect intent, conversation threads, tasks, runs, decisions, and results without relying on filenames or timestamps alone.
- Writes cannot expose partially updated canonical state.
- Unknown versions, malformed records, and unauthorized transitions fail closed.
- Fixtures cover conversational intake, normal completion, human waiting, failure, retry, replacement, and restart recovery.
- The existing bridge behavior remains usable throughout this ticket.

## Full Description

Dory-Wrangler needs a stable protocol before the harness and UI can safely coordinate work. This ticket defines the durable state model that every later component will consume, including the proactive conversation through which the user gives Dory undeveloped intent.

The harness receives broad lifecycle authority but no authority to invent product meaning. Disposable agents may conduct bounded conversations, propose work, and request decisions, while only accepted intent and valid, policy-authorized transitions become canonical. The design must preserve the currently working bridge and session-bound operating model rather than rebuilding them.
