# CLAUDE.md

Claude Code reads this file automatically at session start. It exists only so a
fresh or cleared Claude session activates the canonical contract below. It is a
pointer, not a second copy of that contract.

## Bare `proceed` Activation

In this repository, bare `proceed` or `continue` is an executor instruction, not
a request to resume the conversation. It means read fresh durable state before
acting, even immediately after `/clear`:

```bash
ai-dev discover
```

That is the same installed command the host-level AI Dev activation documents,
and it is the only bootstrap this repository recognises. Then operate as the
executor and follow the canonical contract in
[`skills/executor/SKILL.md`](skills/executor/SKILL.md), whose mechanics live in
`ai_dev_flow/claude_activation.py` and `ai_dev_flow/control_plane.py`. Those are
authoritative for the role, ownership, publication, and handoff rules; do not
restate or reimplement them here. The role itself is provider-neutral; only this
activation path is Claude-specific.

## One Rail Per Session

Read the orchestrator recommendation that discovery reports. If it identifies
exactly one rail for this session, execute only that rail. If no rail is
assigned, or the assignment is materially ambiguous — several rails recommended
for launch with nothing distinguishing this session, or durable state
contradicting the recommendation in a way it does not explain — stop and report
what you read rather than guessing. An unreconciled status the recommendation
explains is normal on takeover, not a stop condition.

## Tasking Precedence

The rail discovery reports is the assignment. This repository keeps no
repository-local tasking rail, so nothing checked in here — including this file —
outranks durable control-plane authorization.
