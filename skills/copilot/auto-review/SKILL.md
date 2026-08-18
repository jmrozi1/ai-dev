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

## Copilot Token Telemetry Evidence

When checkpoint or promotion review expects Copilot token evidence, use the
supported VS Code/Copilot metadata-only OpenTelemetry export. The user-facing
settings are `github.copilot.chat.otel.enabled`,
`github.copilot.chat.otel.exporterType`, `github.copilot.chat.otel.outfile`,
and `github.copilot.chat.otel.captureContent`; conceptually configure them as
`enabled = true`, `exporterType = file`, `outfile =` a local non-repository or
ignored telemetry file, and `captureContent = false`. Token, model, and usage
metadata are sufficient; never enable prompt, response, or tool-content
capture. These are user-level VS Code settings: the executor must not silently
modify them. If they are missing or incorrect, report the exact human action
required and stop where necessary.

If expected token reporting is absent, first check that the configured output
file exists and is nonempty and that the exporter is active. A newly enabled or
restarted Copilot host may require a subsequent Copilot interaction before
telemetry exists. Do not substitute GitHub billing or API-credit data for
Copilot token telemetry, and do not scrape private Copilot SDK or session state
when the supported OTel surface is available. Preserve the distinction among
token consumption, context-window occupancy, and billing or account usage.

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
