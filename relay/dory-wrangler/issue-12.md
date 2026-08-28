# Dory-Wrangler Issue #12 — Verify the first end-to-end internal release

> Transport snapshot only. Canonical source: https://github.com/jmrozi1/dory-wrangler/issues/12
>
> Target repository: `~/dory-wrangler`. This file is not an AI Dev ticket or authorization to modify AI Dev.
>
> A human instruction must select the checkpoint to execute.

## Checkpoints

1. Define the release scenario and evidence manifest from intent through completion.
2. Prove intent shaping and human readiness decisions through the web console.
3. Prove executive decomposition, safe parallel execution, and dependency handling.
4. Prove independent review, bounded remediation, and final completion.
5. Inject agent, bridge, service, and restart failures during active work and prove recovery.
6. Verify systemd user-service operation in the active VM session.
7. Complete a final scope, safety, usability, and evidence audit.

## Acceptance Criteria

- One real internal task completes from captured intent through accepted reviewed output.
- The user performs all required interaction through the two-pane web interface.
- Multiple safe agents run concurrently and conflicting work remains blocked.
- No persistent orchestrator or hidden chat history is required.
- Human decisions, agent results, evidence, and lifecycle state survive service restart.
- Bridge and active-user-session prerequisites are reported accurately.
- Injected failures produce the expected stop, retry, replacement, recovery, or escalation behavior.
- The harness never exceeds its lifecycle authority.
- The final evidence packet demonstrates every acceptance criterion without relying solely on agent assertions.
- The resulting release is usable for one local user on the internal VM.

## Full Description

Validate Dory-Wrangler as an operational internal orchestration system rather than a collection of isolated components. This ticket is the release gate for using the harness and decision console on real work under the current single-user, active-session, VS Code bridge constraints.
