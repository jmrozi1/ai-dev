---
name: auto-review
description: Execute approved AI Dev review evidence gathering and recording mechanics, including active-ticket skill candidate and accepted skill state.
---

# Copilot Auto-Review

Use `change-validation` when gathering additional product evidence. Collect the
smallest review-sufficient evidence requested by the review owner; do not add
full discovery, old/new suite comparisons, or mutation campaigns solely because
the reviewer is fresh.

Execute the repository-facing mechanics of the current AI Dev review stage.
This is the evidence and execution side of auto-review; review policy and
materiality judgment belong to the active task contract or the ChatGPT review
owner.

## Contract

- Read the active tasking rail and verify the current workflow, scope, and
  review stage before acting.
- Run the deterministic evidence helper before review work:
  `skills/copilot/auto-review/scripts/review-evidence --mode checkpoint|promotion`.
- Inspect the changed files, tests, workflow state, ticket skill state, and
  command output named by the active review contract.
- Load or invoke the review skills that the approved task says are applicable.
  Report concrete evidence, test results, candidate/skill section contents, and
  findings without silently broadening applicability.
- Preserve the distinction between observed evidence and review judgment.
- Execute the approved recording mechanics only when the task explicitly
  authorizes recording a pass and the required SHA/workflow identity checks
  succeed.

## Review Composition

For checkpoint review, gather the evidence required by `review-process` and
return the concrete process observations to the review owner. Include the active
ticket's `Skill Candidates` and `Skills` sections when an issue workflow provides
them. When a legacy ticket lacks either section, report the missing section
explicitly; do not convert absence into `None` or infer that no skill work exists.

For promotion review, gather cumulative evidence first, including the current
skill-candidate and accepted-skill ticket state. Execute only the candidate
reviews the task has authorized after applicability is decided. Do not load
`frontend-design-review` merely because it is configured; the issue surface must
show relevant GUI or front-end design work.

Copilot does not decide whether a candidate stays, is rejected, is promoted into
`Skills`, or whether accepted skill work is sufficient. Those judgments belong
to `review-process` / `skill-authoring` under the ChatGPT review owner. Copilot
may edit ticket sections only when the active task provides the exact approved
disposition or updated content.

## Ticket Skill-State Evidence

`review-evidence` should provide one compact `Ticket Skill State` section for
active issue workflows. It should surface, without interpretation:

- the current `Skill Candidates` section body;
- the current `Skills` section body;
- an explicit missing-section diagnostic for legacy tickets;
- an explicit unavailable diagnostic when the active ticket cannot be read.

Do not emit the entire ticket body merely to expose these sections. Do not turn
the evidence helper into a ticket mutator or skill-disposition engine.

## Copilot Token Telemetry Evidence

For meaningful checkpoint and promotion reviews, `review-evidence` automatically
attempts one fresh metadata-only Copilot OTel sample and reconciliation before
rendering the existing AI Usage evidence. The orchestrator does not need to ask
for a separate telemetry refresh. Available telemetry is management evidence
included automatically; unavailable or insufficient telemetry is reported
concisely and remains non-blocking. Genuine collection failure is also reported
concisely and remains non-blocking, without being converted into apparent
success. Telemetry availability, token counts, and cost values never determine
review PASS/FAIL and are not executor optimization targets.

Preserve the existing report wording for attribution, unresolved pricing or
input-cache information, and scenario values. Session-scoped or unattributable
usage must not be presented as issue-attributable, and scenarios must not be
presented as actual totals or bounds. Keep collection metadata-only under the
existing privacy contract; never enable prompt, response, or tool-content
capture. Ordinary Flow lifecycle commands remain telemetry-independent.

The user-facing VS Code settings are `github.copilot.chat.otel.enabled`,
`github.copilot.chat.otel.exporterType`, `github.copilot.chat.otel.outfile`,
and `github.copilot.chat.otel.captureContent`. These are user-level settings:
the executor must not silently modify them. If telemetry is unavailable, rely
on the concise status emitted by review evidence rather than adding a manual
refresh instruction or substituting GitHub billing/API-credit data.

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
task must authorize that decision, every applicable review must have passed,
ticket skill state must have been judged closure-ready by the review owner, and
the recorder must bind the result to the current scratch SHA and workflow
identity. Do not invent review policy, add candidate dispositions, or convert a
failed or ambiguous result into a pass.

## Shared Mechanics

`review-evidence` and `record-promotion-review` are owned by this Copilot
package. They gather or persist state; they do not replace ChatGPT's review
judgment or authorization contract.
