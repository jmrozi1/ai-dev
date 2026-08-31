---
name: requirements-driven-development
description: Keep requirements as current intent and drive implementation with objective evidence of their satisfaction.
---

# Requirements-Driven Development Skill

Use requirements as the primary expression of current product intent. Code,
tests, tickets, plans, and observed behavior support requirements; they do not
replace them.

## When To Use This Skill

Use this skill when defining, refining, implementing, validating, reviewing,
or changing behavior that needs an explicit statement of intended outcome.

TDD is compatible with this skill, but remains subordinate to requirements:
tests are one form of evidence that requirements are satisfied.

## Express Current Intent

State the required outcome, the evidence that would objectively show it is
satisfied, and the behavior that would violate it. A requirement should give a
competent engineer or agent enough direction to implement and validate the
work without inventing material product intent.

High-level requirements may be recursively refined. Stop refining when the
outcome, satisfaction evidence, violating behavior, and bounded implementation
are clear at the level being executed. A requirement is not refined enough when
unresolved ambiguity could materially change externally observable behavior or
the evidence that establishes satisfaction. Implementation choices may remain
open when they do not require inventing material product intent and do not
conflict with established project conventions.

Current requirements describe current intent, not the history of how a team
arrived there. Do not create requirement IDs, storage schemes, parent/child
schemas, test annotations, coverage formulas, dashboards, or traceability
tooling unless the project explicitly requires them.

## Work From Requirements

Treat tickets as temporary work slices against requirements, rather than as
the source of product intent. Before implementation, determine which current
requirements the work must satisfy. Keep conceptual parent and child
traceability as lightweight as the work permits; do not impose a formal
traceability system.

When implementation, investigation, or feedback reveals newly needed behavior,
write it as a new or refined requirement before adding it to implementation
scope. Escalate material product, scope, architecture, or permission decisions
instead of silently inventing intent.

## Use Evidence Deliberately

Choose evidence appropriate to the requirement and risk. Evidence can include
unit, integration, or end-to-end tests; static checks; explicit manual
validation; runtime observations; logs; metrics; or other objective results.
Do not reduce validation to unit-test coverage alone.

Before relying on a failure or other negative observation as evidence, confirm
that it was caused by the requirement or proof obligation being evaluated.
Compilation, setup, configuration, environment, harness, or unrelated failures
do not establish the intended negative case merely because the check failed.

Record only the evidence needed to establish the current result and remaining
uncertainty. Do not turn requirements work into a process diary or append-only
history.

## Distinguish Confidence Levels

For mature projects, distinguish among:

- confirmed requirements: explicitly accepted current intent;
- provisional or inferred requirements: plausible intent that still needs
  confirmation; and
- observed behavior: implementation evidence that may inform intent but does
  not prove it.

Inferred requirements may remain local or untracked when that is sufficient.
Do not promote observed behavior into a confirmed requirement without a basis
for doing so.

## Review And Handoff

When handing work off, state the requirements addressed, the evidence obtained,
any unresolved uncertainty, and decisions needed. Keep the handoff focused on
current work and next decisions rather than a transcript of execution.

## ChatGPT Interaction

When ChatGPT uses this shared skill for routine requirements interpretation,
announce `Skill: requirements-driven-development`, or include it in a
responsibility-ordered composed chain, and continue without extra gating. When
ChatGPT begins substantive requirements design or refinement that could
materially change intent, announce the skill or responsibility-ordered composed
chain and briefly summarize the outcome, evidence, and violating behavior it
will refine before continuing.

This instruction is scoped to ChatGPT use. It does not change Copilot or Work
requirements execution.

Routine interpretation remains announcement-only. For a new invocation or a
materially changed skill chain, re-announce the active skill at the start of the
response.