---
name: follow-documentation-for-task
description: >-
  Use when the user wants to follow a documented procedure interactively, one
  step at a time, while the agent performs each step when possible and records
  only exceptions that reveal documentation or project-instruction gaps. Maintain
  a simple execution file with step status and notes, stop on uncertainty instead
  of independently solving around missing guidance, and resume after the user
  resolves or approves the next action.
---

# Follow Documentation For Task

Follow a concrete documented procedure interactively, one meaningful step at a time. Perform the step when possible, but treat inability to proceed confidently from the procedure and established project instructions as evidence rather than something to silently work around.

## Core behavior

1. Start from a concrete ordered procedure supplied by the user or produced by a documentation review.
2. Persist the procedure to an execution file before beginning.
3. Work on exactly one meaningful step at a time.
4. Perform the current step when the agent can do so safely and with sufficient documented or established project guidance.
5. If the step is unclear, incomplete, or requires information that is not available, stop and tell the user what is missing.
6. Do not independently research, infer, or invent a workaround merely to make progress when that would hide a documentation or project-instruction gap.
7. After the step is resolved, update its status and notes, then stop before beginning the next step unless the user explicitly asks to continue.

The purpose is not to prove that the agent can eventually solve the task. The purpose is to determine whether the documented procedure plus established project instructions are sufficient to perform it reliably.

## Execution file

Use a user-supplied path when provided. Otherwise write:

`./tmp/documentation-execution-<task-slug>.md`

relative to the target project. Create `./tmp/` when needed and overwrite the same task-derived execution file by default when intentionally starting the task over.

Use a minimal structure:

```markdown
# <Task>

## Step 1 — <step title>

Status: Not started

Notes:

## Step 2 — <step title>

Status: Not started

Notes:
```

Allowed statuses:

- Not started
- In progress
- Complete
- Skipped
- Blocked

Use `Blocked` only when the step cannot currently continue. Change it when the blocker is resolved.

## Notes policy

Notes are exception-only.

Do not add notes merely to record that a successful step worked, restate the procedure, summarize commands, or narrate normal execution.

Add concise notes only when something materially useful was learned, such as:

- a missing prerequisite;
- an undocumented decision or value;
- an ambiguous or misleading instruction;
- a required recovery or workaround;
- a user answer needed to continue;
- a project-specific fact the agent needed but `CLAUDE.md` or equivalent project instructions did not provide;
- or another condition showing that the documented path or agent instructions were insufficient.

Keep all such information in the single `Notes:` field for the affected step. Do not create separate expected-result, actual-result, evidence, analysis, or finding sections.

## Uncertainty and missing guidance

When the current step cannot be performed confidently from the procedure and already-established project knowledge:

1. Stop before guessing or broad investigation.
2. State the specific uncertainty or missing information.
3. Ask the user for the information, decision, or authorization needed to continue.
4. Identify whether the gap appears to belong in:
   - project documentation, because an engineer following the task should know it;
   - `CLAUDE.md` or equivalent agent instructions, because the AI needs project-specific operating knowledge;
   - or both.
5. If the user wants the relevant documentation or project instructions updated during the walkthrough, make only the targeted approved change.
6. Record the useful conclusion in the current step's `Notes:` field.
7. Resume the same step rather than skipping ahead.

Do not conceal a gap by finding the answer elsewhere and continuing as though the original guidance was sufficient.

## Step execution

For each step:

1. Mark it `In progress` before acting.
2. Perform the documented action when possible.
3. If human action, input, authorization, credentials, physical access, or an unresolved choice is required, ask the user and stop.
4. If execution exposes a guidance gap, follow the uncertainty rules above.
5. Mark the step `Complete` only when its required action is actually complete.
6. Mark it `Skipped` only when the user intentionally chooses not to perform it.
7. Leave `Notes:` empty when nothing went wrong or needed clarification.
8. Stop before the next step unless the user explicitly asks to continue.

Never claim a step succeeded without observed evidence appropriate to that step.

## Relationship to documentation review

This skill complements `review-documentation-for-task` and its specializations:

- the review can produce the dependency-correct procedure;
- this skill follows that procedure in the real environment;
- the execution file captures only places where reality required extra information or intervention;
- `review-documentation-for-task-propose-changes` can later use those notes as high-signal evidence for documentation changes.

Unlike `review-documentation-for-task-simulate-reader`, this skill performs real actions when possible. Unlike a general troubleshooting workflow, it must not solve around missing guidance in ways that hide documentation or agent-instruction deficiencies.

## Completion criteria

The walkthrough is complete when every procedure step is `Complete` or intentionally `Skipped`, unresolved blockers are visible, exception notes capture only meaningful guidance gaps, and any documentation or `CLAUDE.md` issues discovered during execution have been surfaced to the user rather than silently bypassed.
