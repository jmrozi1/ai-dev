# Dory-Wrangler Issue #11 — Add supervision, anomaly detection, and recovery

> Transport snapshot only. Canonical source: https://github.com/jmrozi1/dory-wrangler/issues/11
>
> Target repository: `~/dory-wrangler`. This file is not an AI Dev ticket or authorization to modify AI Dev.
>
> A human instruction must select the checkpoint to execute.

## Checkpoints

1. Define a failure and anomaly taxonomy with the observable evidence available for each class.
2. Add structured run heartbeats, log cursors, progress markers, and exact process and bridge liveness checks.
3. Detect deterministic failures including process exit, bridge loss, missing output, malformed output, deadline breach, and abandoned ownership.
4. Add bounded diagnostic executive tasks for ambiguous behavior such as repetition, contradiction, or apparent lack of progress.
5. Define and implement policy-driven prod, stop, retry, replace, clean-up, and human-escalation actions.
6. Reconcile orphaned processes and durable state after harness, web-service, VS Code, and VM-session interruptions.
7. Build a fault-injection matrix covering each detection and recovery path.
8. Complete an end-to-end audit proving that supervision never invents semantic conclusions.

## Acceptance Criteria

- Every supported failure class has an observable signal, response policy, and durable outcome.
- Deterministic failures are handled by the harness without waiting for an agent to remember to stop.
- Ambiguous semantic anomalies may be investigated by a bounded disposable agent, but its conclusion is validated and cannot directly exercise harness authority.
- Prodding, retries, replacements, and diagnostic loops are bounded and idempotent.
- A stalled or repeating agent cannot silently consume work indefinitely.
- Bridge or user-session loss produces a clear recoverable state.
- The web server remains responsive while individual agents stall or fail.
- Orphaned processes and claims are either safely reattached, terminated, or escalated without duplicate work.
- Fault injection proves recovery from failures at every lifecycle boundary.
- Unknown anomalies fail closed and reach the human-attention queue with the available evidence.

## Full Description

This is the major reliability ticket. Dory-Wrangler must recognize when observable behavior diverges from expected lifecycle progress and take the safest authorized response.

The harness should own mechanical detection and recovery because it is the only component assumed to remain dependable. Where a symptom requires interpretation—for example, determining whether output has begun repeating—a disposable diagnostic executive task may analyze the evidence and propose a reusable watcher or immediate disposition, but the harness validates and applies any resulting action.
