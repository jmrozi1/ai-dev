# Dory-Wrangler Issue #12 — Verify the first end-to-end internal release

> Transport snapshot only. Canonical source: https://github.com/jmrozi1/dory-wrangler/issues/12
>
> Target repository: `~/dory-wrangler`. This file is not an AI Dev ticket or authorization to modify AI Dev.
>
> A human instruction must select the checkpoint to execute.

## Checkpoints

1. Define the release scenario and evidence manifest from a rough proactive-console prompt through reviewed completion.
2. Prove multi-turn Dory conversation, intent shaping, readiness, and launch authorization through the web interface.
3. Prove executive decomposition, safe parallel execution, and dependency handling.
4. Prove independent review, bounded remediation, and final completion.
5. Inject agent, bridge, AI-availability, service, and restart failures during active work and prove recovery.
6. Verify systemd user-service operation in the active VM session.
7. Complete a final scope, safety, usability, and evidence audit.

## Acceptance Criteria

- One real internal task begins as a rough prompt in the bottom Dory console and completes through accepted reviewed output.
- Dory refines the prompt through a coherent multi-turn conversation before executable requirements are authorized.
- The user performs all required proactive and reactive interaction through the web interface.
- Multiple safe agents run concurrently and conflicting work remains blocked.
- No persistent orchestrator, individual provider session, or hidden chat history is required.
- Human decisions, accepted intent, conversation continuity, agent results, evidence, and lifecycle state survive service restart and agent replacement.
- Bridge and active-user-session prerequisites are reported accurately.
- AI unavailability is reported clearly without making durable non-AI state inaccessible.
- Injected failures produce the expected stop, retry, replacement, recovery, or escalation behavior.
- The harness never exceeds its lifecycle authority.
- The final evidence packet demonstrates every acceptance criterion without relying solely on agent assertions.
- The resulting release is usable for one local user on the internal VM.

## Full Description

Validate Dory-Wrangler as an operational internal replacement for the external product-shaping and development-orchestration loop, rather than a collection of isolated components. The release must accept a rough idea through the proactive console, refine it into authorized work, execute and review that work, and return genuine decisions through the attention workspace.

This ticket is the release gate for using the complete loop under the current single-user, active-session, Gemma, and VS Code bridge constraints.
