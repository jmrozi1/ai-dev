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

### No-Argument Inference

Run the recorder with **no arguments** when the current Flow repository state is
safe and unambiguous:

```bash
skills/copilot/auto-review/scripts/record-promotion-review
```

No-argument invocation is the normal path. It writes the same promotion-review
PASS record as the explicit form only when all of these are true:

- An active Flow issue workflow is configured in `.ai-dev/workflow.json`
- The workflow is issue-based (not patch-based)
- The active issue number is unambiguous
- Valid main/scratch branch metadata exists
- The current branch exactly matches the configured `scratch` branch
- The working tree is clean (no staged or unstaged changes)
- No Git operation is active (no rebase, merge, cherry-pick, etc.)
- `HEAD` is exactly the same commit as the scratch branch tip

**Success confirmation**: The recorder explicitly outputs:

```
recorded promotion review pass for .ai-dev/promotion-review.json
```

and creates `.ai-dev/promotion-review.json` with fields:
- `result: "pass"`
- `activeIssueNumber: <active-issue>`
- `scratchCommit: <exact-current-scratch-sha>`
- `mainBranch`, `scratchBranch`: configured metadata

**Do not claim recording succeeded merely because the command ran**. Verify the
explicit success output.

### Unsafe Inference

If any precondition fails, the recorder exits nonzero and does not write:

```
record-promotion-review: no promotion-review record was written
Use: record-promotion-review --issue <number> --commit <sha>
```

The exact failure reason appears before the usage line. No record is created,
and any pre-existing `.ai-dev/promotion-review.json` remains byte-for-byte
unchanged.

### Explicit Form for Recovery

When inference is unsafe or you need to override the inferred target:

```bash
skills/copilot/auto-review/scripts/record-promotion-review --issue <number> --commit <sha>
```

Use only for deliberate recovery/override, not as a normal path. This form
requires explicit argument validation and does not weaken exact-SHA binding.

### No-Write Failure

Do not proceed with promotion if the no-argument recorder fails. Report the
failure output to the active task owner and confirm recovery steps are taken
before retry.

### Promotion Gate Integrity

Never weaken or bypass exact-SHA promotion gating. The `flow-promote` command
requires a valid PASS record whose `scratchCommit` and workflow identity exactly
match the current state. This checkpoint does not alter that requirement.

### Recording Authorization

Do not record a promotion pass based only on successful commands. The active
task must authorize that decision, every applicable review must have passed, and
the recorder must bind the result to the current scratch SHA and workflow
identity. Do not invent review policy, add candidates, or convert a failed or
ambiguous result into a pass.

## Shared Mechanics

`review-evidence` and `record-promotion-review` are owned by this Copilot
package. They gather or persist state; they do not replace ChatGPT's review
judgment or authorization contract.
