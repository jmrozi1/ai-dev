---
name: change-validation
description: Select proportionate test and review evidence for code, configuration, or skill changes when validation scope, compatibility, expensive environments, or proof obligations require judgment. Do not use for non-development tasks.
---

# Change Validation

Make validation cost scale with the novelty and blast radius of the change, not
the age of the repository or the number of historical checkpoints.

## Select The Smallest Sufficient Tier

Start with the lowest tier that can falsify the changed behavior. Move upward
only when the change crosses that tier's boundary or lower-tier evidence exposes
a material uncertainty.

1. **Local:** static checks and the directly changed unit or module tests.
2. **Contract:** tests for a changed interface, schema, persistence boundary, or
   adapter, including both sides of that boundary when necessary.
3. **Integration:** several modules, processes, services, or an expensive build
   whose composition is part of the changed claim.
4. **Full or live regression:** the whole product, real environment, or
   end-to-end path.

Full or live regression is warranted at a named-checkpoint, promotion, or
release boundary; for an explicitly cross-cutting/high-risk change; when a
shared foundation changed with no narrower contract surface; or when targeted
evidence reveals a failure outside the expected boundary. A numeric checkpoint,
fresh reviewer, large existing suite, or prior use of full discovery is not by
itself a reason to run it again.

Record the selected tier and the changed boundary that justifies it. Do not add
lower and higher tiers mechanically when the higher tier already subsumes the
needed evidence, and do not run an old checkpoint's full suite merely to create
a comparison unless the current failure surface cannot otherwise be interpreted.

## Treat Accepted Tests As Memory

Once an accepted automated test protects an invariant at the right boundary,
future work normally runs that test. Do not reconstruct the invariant's original
manual proof, historical fixture archaeology, or mutation campaign unless the
boundary changes, the test fails, or its kill power is materially disputed.

Use deliberate-breakage or mutation evidence for a novel semantic contract, a
new test whose sensitivity is uncertain, or a focused reviewer hypothesis. It
is not a default certification layer. An executor-authored control and an
independent reviewer control should not both be required without distinct
questions.

## Review The Novel Claim

A reviewer first evaluates the executor's commands, outputs, provenance, and
changed boundary. Re-run evidence only when it is missing, stale, not
reproducible, environment-sensitive in a material way, or when an independent
hypothesis needs testing. Normally attack the riskiest novel claim or likely
failure mode instead of duplicating the executor's entire certification.

Passing accepted regression tests is evidence; the reviewer need not re-derive
why every test exists. Escalate only the evidence gap that prevents judgment.
An evidence wrapper is not sacred: if equivalent evidence proves its unique
safety and correctness properties, do not require another expensive run merely
to change packaging.

## Prefer Current Contracts Over Pre-Release History

Before the first supported release, do not preserve an internal representation,
interface, fixture, state format, or behavior unless a named real external
consumer or valuable durable user data requires it. Git history and an earlier
checkpoint are not consumers.

When no such consumer or data exists, prefer a one-time migration, state
regeneration, caller update, direct replacement, and deletion of obsolete
branches, adapters, fixtures, and tests. When compatibility is retained, name
the consumer or data and the required lifetime in the handoff.

## Allow Directly Adjacent Simplification

A narrow assignment may delete or simplify machinery directly superseded by
the requested change when all of these are true:

- the simplification is inside the same changed boundary;
- it makes the implementation smaller or removes a parallel representation;
- current required behavior remains protected; and
- it does not become unrelated cleanup, redesign, or migration.

Report the deletion or contract replacement as part of the change. Stop for a
material product, scope, architecture, permission, or external compatibility
decision; do not preserve obsolete code merely because deleting it was not
spelled out separately.

## Keep Cost Telemetry Lightweight

For non-trivial development handoffs, report when observable:

`change size/paths -> modules or boundaries -> selected tier -> tests/builds/live passes -> validation runtime -> total agent runtime`

Use this to diagnose amplification, not score velocity or trigger autonomous
intervention. Missing time is unavailable, not zero.

Expected feedback bands are guidance:

- local validation for an ordinary small change: usually under 5 minutes;
- bounded contract or integration validation: usually under 15 minutes;
- a single validation step over 15 minutes, total validation over 20 minutes, or
  an ordinary small assignment over 30 minutes: expensive.

Crossing a band does not fail correct work. Before paying another expensive
pass, finish the safe unit, report where cost went, and let the executor or
orchestrator reduce the loop, batch uniquely valuable evidence, or explicitly
justify the higher tier.

## Prevent Proof Ratchets

Route a material finding to one smallest durable outcome: product fix,
appropriate automated test, current requirement, or reusable skill/process
change. Once automation supersedes a prose obligation, remove the repeated prose
from future rails and live state. Preserve only unresolved or still-operative
context; Git and closed issues retain history.

At promotion, and no more than monthly for an actively changing project, sample
up to five recent ordinary changes. Check whether validation tier and runtime
still match their blast radius, whether reviewers duplicated certification,
and whether incident prose or compatibility paths can now retire. Record only
actions that remain current; do not create a dashboard or history database.

## ChatGPT Interaction

When ChatGPT intentionally activates this shared skill, announce
`Skill: change-validation`, or include it in a responsibility-ordered composed
chain, and continue without extra gating.
