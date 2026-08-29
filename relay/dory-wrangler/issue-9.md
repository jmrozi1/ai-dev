# Dory-Wrangler Issue #9 — Automate independent review and remediation routing

> Transport snapshot only. Canonical source: https://github.com/jmrozi1/dory-wrangler/issues/9
>
> Target repository: `~/dory-wrangler`. This file is not an AI Dev ticket or authorization to modify AI Dev.
>
> A human instruction must select the checkpoint to execute.

## Checkpoints

1. Define policies that require or permit independent review.
2. Build complete review packets from requirements, authorized scope, changes, and evidence.
3. Launch a fresh reviewer and capture a structured review outcome.
4. Route accepted work, bounded remediation, and unresolved judgment through explicit state transitions.
5. Prevent self-review, circular remediation, and unbounded review loops.
6. Verify acceptance, findings, reviewer failure, conflicting evidence, and human escalation paths.

## Acceptance Criteria

- Required reviews are launched automatically by the harness from explicit policy.
- A session that performed the reviewed work cannot satisfy a freshness-required review.
- Reviewers judge requirements and evidence rather than trusting executor claims.
- Accepted work advances only after the required review outcome is durable.
- Findings create bounded remediation work without silently changing the original requirements.
- Retry and remediation counts are bounded.
- Conflicting or ambiguous outcomes enter the human-attention queue.
- Complete evidence is retained across review and remediation cycles.

## Full Description

Turn review into an ordinary orchestrated task rather than a manual session-management exercise. The harness owns routing and loop bounds; independent reviewers provide bounded judgment over the actual output and evidence.
