---
name: flow
description: Resolve AI Dev repository, ticket, workspace, and control-plane rail identity from a Claude session; route lifecycle intents to the shared deterministic Flow runtime.
---

# Claude Flow

Use this skill when a Claude session must act as an AI Dev executor in a Git
repository. It supplies discovery and routing only. The deterministic lifecycle
runtime lives once in `ai_dev_flow` and is not reimplemented here, and the
collaboration contract lives once in the shared `executor` skill.

## Activation

A bare `proceed` or `continue` in a supported repository is an executor
instruction, not conversational continuation and not a request to recall a
previous task from Claude memory. Resolve durable state before acting:

```bash
ai-dev discover
```

This reports canonical repository identity, the active ticket, the control-plane
scope, and the single authorized rail. Follow only that rail.

## Discovery Contract

| Need | Source |
| --- | --- |
| Repository identity | `git remote get-url origin`, normalized to `owner/repo` |
| Project namespace | the repository name |
| Ticket | `activeIssueNumber` in Flow workflow state, as `issue-<n>` |
| Workspace | the Issue #50 claim registry |
| Rail | the single `ready` rail in the control-plane scope |

`ai-dev identity` resolves identity alone, which is useful when control-plane
data is unavailable or malformed and you need to prove what the repository is.

## Fail Closed

Discovery stops with an actionable diagnostic and no speculative execution when
repository identity is unresolvable, no Flow ticket is active, the control-plane
cache is missing or unfetchable, the project/ticket namespace does not exist, or
zero or more than one rail is ready.

When it stops, report exactly what it reported. Never substitute Claude memory,
product documentation, an issue comment, a product-local handoff file,
`.ai-dev/tasking.md`, or a Flow checkpoint number for durable authorization.

## Lifecycle Routing

Lifecycle commands remain the shared Flow runtime's. Route intents to the
installed Flow helpers rather than reimplementing them, and read the shared
`executor` skill for the role contract, publication rules, and stop conditions.
