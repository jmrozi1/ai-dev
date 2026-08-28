# Dory-Wrangler Issue #3 — Integrate the managed agent runner

> Transport snapshot only. Canonical source: https://github.com/jmrozi1/dory-wrangler/issues/3
>
> Target repository: `~/dory-wrangler`. This file is not an AI Dev ticket or authorization to modify AI Dev.
>
> A human instruction must select the checkpoint to execute.

## Checkpoints

1. Capture the proven bridge invocation as a project-owned adapter without changing the VS Code bridge itself.
2. Create durable run identities and bounded input packets.
3. Capture process identity, stdout, stderr, exit status, timing, and structured completion state.
4. Add explicit termination and cleanup behavior.
5. Prove independent concurrent agent launches.
6. Integrate run creation and completion with the harness scheduler.

## Acceptance Criteria

- The harness launches agents through the already proven internal bridge.
- Every launch has a unique durable run identity and exact input packet.
- Success, model failure, bridge failure, process failure, and termination are distinguishable.
- Multiple independent agents can run concurrently without serializing through the web server.
- Termination targets the correct process tree and records the outcome.
- Raw output is preserved even when structured parsing fails.
- Token counting is not required.
- The active user session and initialized VS Code bridge remain explicit operating prerequisites.

## Full Description

Move the existing proof of agent execution behind a managed lifecycle boundary. This ticket does not redesign or automate bridge initialization; it makes launches observable, attributable, concurrent, and controllable by the harness.
