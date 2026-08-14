---
name: auto-review
description: Execute approved AI Dev review evidence gathering and recording mechanics.
---

# Copilot Auto-Review

Execute the repository-facing mechanics of the current AI Dev review stage.
This is the evidence and execution side of auto-review; review policy and
materiality judgment belong to the active task contract or the ChatGPT review
owner.

## Contract

- Read the active tasking rail and verify the current workflow, scope, and
  review stage before acting.
- Run the deterministic evidence helper before review work:
  `skills/copilot/auto-review/scripts/review-evidence --mode checkpoint|promotion`.
- Inspect the changed files, tests, workflow state, and command output named by
  the active review contract.
- Load or invoke the review skills that the approved task says are applicable.
  Report concrete evidence, test results, and findings without silently
  broadening applicability.
- Preserve the distinction between observed evidence and review judgment.
- Execute the approved recording mechanics only when the task explicitly
  authorizes recording a pass and the required SHA/workflow identity checks
  succeed.

## Review Composition

For checkpoint review, gather the evidence required by `review-process` and
return the concrete process observations to the review owner.

For promotion review, gather cumulative evidence first. Execute only the
candidate reviews the task has authorized after applicability is decided. Do not
load `frontend-design-review` merely because it is configured; the issue
surface must show relevant GUI or front-end design work.

## Recording Boundary

The canonical current-state recorder is:

`skills/copilot/auto-review/scripts/record-promotion-review`

Do not record a promotion pass based only on successful commands. The active
task must authorize that decision, every applicable review must have passed, and
the recorder must bind the result to the current scratch SHA and workflow
identity. Do not invent review policy, add candidates, or convert a failed or
ambiguous result into a pass.

## Shared Mechanics

`review-evidence` and `record-promotion-review` are owned by this Copilot
package. They gather or persist state; they do not replace ChatGPT's review
judgment or authorization contract.
