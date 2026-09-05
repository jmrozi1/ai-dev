# Dory-Wrangler Issue #8 — Add parallel execution and resource-safety policies

> Transport snapshot only. Canonical source: https://github.com/jmrozi1/dory-wrangler/issues/8
>
> Target repository: `~/dory-wrangler`. This file is not an AI Dev ticket or authorization to modify AI Dev.
>
> A human instruction must select the checkpoint to execute.

## Checkpoints

1. Represent proposed child tasks, dependencies, scopes, and resource claims durably.
2. Validate executive parallelization proposals against authorization and lifecycle policy.
3. Add repository, workspace, environment, and other exclusive-resource ownership.
4. Launch nonconflicting eligible tasks concurrently.
5. Block and explain shared-resource contention.
6. Reconcile completion and dependency release without implicit merging or conflict resolution.
7. Prove safe concurrency and hard-block behavior with adversarial schedules.

## Acceptance Criteria

- Parallel work begins only from a validated proposal.
- Shared repository or resource contention is a hard blocker.
- Two live executors cannot mutate the same protected workspace or scope.
- Dependencies prevent premature launch and release deterministically after proven completion.
- The harness never resolves implementation conflicts by assumption.
- Concurrent safe tasks make observable progress independently.
- Repeated polling, restart, or delayed output cannot duplicate child tasks or resource claims.
- Tests cover simultaneous proposals, races, stale ownership, partial completion, and blocked dependencies.

## Full Description

Enable useful parallelism without transferring spawn authority to agents. Executive tasks identify opportunities; the harness validates scopes, dependencies, and resources before creating and launching bounded executor work.
