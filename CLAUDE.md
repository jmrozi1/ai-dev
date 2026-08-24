# CLAUDE.md

Claude Code reads this file automatically at session start. It exists only so a
fresh or cleared Claude session activates the canonical contract below. It is a
pointer, not a second copy of that contract.

## Bare `proceed` Activation

In this repository, bare `proceed` or `continue` is an executor instruction, not
a request to resume the conversation. It means read fresh durable state before
acting, even immediately after `/clear`:

```bash
python -m ai_dev_flow.control_plane config
python -m ai_dev_flow.control_plane status
python -m ai_dev_flow.control_plane rail --rail <rail-id>
```

Then operate as the executor and follow the canonical contract in
[`skills/copilot/executor/SKILL.md`](skills/copilot/executor/SKILL.md), whose
mechanics live in `ai_dev_flow/control_plane.py`. Those are authoritative for
the role, ownership, publication, and handoff rules; do not restate or
reimplement them here. The role itself is provider-neutral; only this activation
path is Claude-specific.

## One Rail Per Session

Read the orchestrator recommendation that `status` prints. If it identifies
exactly one rail for this session, execute only that rail. If no rail is
assigned, or the assignment is materially ambiguous — several rails recommended
for launch with nothing distinguishing this session, or a status the helper
flagged as unreconciled — stop and report what you read rather than guessing.

## Tasking Precedence

When a control plane is configured, the authorized rail is the assignment and
the local `.ai-dev/tasking.md` is not canonical; prefer the rail wherever the
two disagree. When no control plane is configured, `.ai-dev/tasking.md` remains
the rail and no external coordination is required.
