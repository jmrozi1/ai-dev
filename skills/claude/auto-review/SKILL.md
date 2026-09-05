---
name: auto-review
description: Route Claude checkpoint and promotion review to the shared deterministic review-evidence and recording helpers without duplicating review judgment.
---

# Claude Auto Review

Use this skill when a Claude executor session reaches a checkpoint or promotion
review boundary. It routes to existing deterministic helpers; it does not define
review judgment, which lives in the shared `review-process` skill.

## Evidence First

Generate review evidence from the workspace under review with the canonical
source-controlled helper, and preserve its compact output as the evidence of
record. Do not summarize from memory and do not hand-author evidence.

## Recording

Record a promotion review pass only through the canonical no-argument recorder,
and only when the deterministic evidence matches the reviewed workflow and exact
scratch commit. Never use an explicit override form to get past a refusal, and
never bypass the recorder's inference checks.

## Interpreter Selection

Invoke helpers through the installed AI Dev launcher so Python is chosen by the
repository bootstrap selector. Do not assume a bare `python3` is a real
interpreter: on Windows it can resolve to the Microsoft Store alias, which
produces failures that are easily misread as repository state corruption.

## Stop Conditions

Stop and report when evidence contradicts the accepted review state, when the
recorder refuses, or when publication access fails. A review that cannot be
recorded is not a review that passed.
