---
name: ticket-creation
description: Create and validate tickets using the canonical repository ticket model and provider-neutral creation contract.
---

# Ticket Creation Skill

Use this skill when creating a new task record through the repository’s ticket
system. Canonical ticket data follows the normalized model implemented in
`ai_dev_flow/tickets.py` and the local provider in
`ai_dev_flow/ticket_providers.py`.

## Canonical Ticket Format

The canonical ticket payload is a JSON object with a `reference` block plus a
normalized storage shape. The required form is:

```json
{
  "reference": {
    "provider": "local",
    "ticketId": "42",
    "path": ".ai-dev/tickets"
  },
  "title": "Implement the scoped fix",
  "lifecycleState": "open",
  "workflowState": "inactive",
  "body": "## Checkpoints\n\n- [ ] first-checkpoint: Define the first planned slice.\n\n## Acceptance Criteria\n\n- Observable outcome is testable.\n\n## Full Description\n\nDescribe the task and its scope.",
  "acceptanceCriteria": [
    "Outcome is observable and testable.",
    "The change remains within the assigned scope."
  ],
  "labels": ["ops"],
  "createdAt": "2026-08-17T00:00:00Z",
  "updatedAt": "2026-08-17T00:00:00Z"
}
```

Rules:

- `reference.provider` is one of `local` or `github`.
- `reference.ticketId` is the canonical ticket identifier, and local ticket files
  must be stored as `<id>.json` without leading zeros.
- Local tickets use a repository-relative `path` such as `.ai-dev/tickets`.
- GitHub tickets require `repository` instead of `path`, and are validated
  against the repository owner/name format. GitHub URLs are optional and must be
  valid HTTP(S) URLs.
- `title` is required and must be non-empty after trimming.
- The title must stand on its own in a backlog, notification, cross-project
  list, or `/status` output. It should identify the affected behavior or system
  and the intended change with enough scope to distinguish the work from
  adjacent tickets.
- Do not require project prefixes or a rigid naming grammar. Do not add title
  scores, taxonomies, templates, or another summary field.
- `lifecycleState` must be `open` or `closed`.
- `workflowState` must be `inactive`, `active`, or `blocked`.
- `acceptanceCriteria` and `labels` are arrays of strings, not free-form text.
- Optional timestamps are ISO-8601 UTC values ending in `Z`.

## Creation Contract

When creating a ticket:

- Resolve the configured ticket provider from `.ai-dev/config.json`.
- Prefer the repository’s existing provider boundary and validation model instead
  of inventing a custom ticket schema.
- For local provider creation, write exactly one JSON file for the new numeric
  ticket ID; do not reuse or overwrite an existing ID.
- Keep the ticket title self-contained and implementation-oriented; for
  example, prefer `Add issue-level AI usage accounting and management
  reporting` over `Add reporting`.
- When a ticket body is provided, use this section order: `Checkpoints`,
  `Acceptance Criteria`, then `Full Description`. Do not create or require an
  `Executive Summary` section.
- Do not add unsupported fields or alternate naming conventions.
- If the configured provider cannot create the ticket, report the provider error
  and stop rather than inventing a ticket record outside the canonical format.

## Named Checkpoint Roadmap Contract

Ticket checkpoints are the canonical current implementation roadmap for a
work item. They are not a second, hidden, or runtime-local source of intent.

Use this minimal durable contract:

- A ticket checkpoint is a named roadmap item with a `name` and `description`.
- The first incomplete named checkpoint is the current roadmap checkpoint.
- Before intentionally deviating from the roadmap, update the ticket checkpoint
  list first.
- Acceptance Criteria remain independent of checkpoint state; ACs define the
  required outcomes while checkpoints define the predicted route.
- Completing a named checkpoint is the normal boundary for creating a Flow
  checkpoint commit and running checkpoint review.
- Flow numeric `checkpoint` state remains deterministic execution state; it is
  not the authoritative named-roadmap index.
- Review fixes or retries may create additional Flow checkpoint commits without
  advancing the named roadmap.
- Do not duplicate checkpoint names or descriptions into Flow workflow state.

A minimal representation is a ticket section such as:

```markdown
## Checkpoints
- [ ] checkpoint-name: short description of the next planned slice
- [ ] next-checkpoint: short description of the next planned slice after that
```

The first unchecked item is the active roadmap checkpoint. When the route
changes materially, revise the checkpoint list in the ticket before continuing
with the different path.

## Scope of This Checkpoint

This checkpoint defines the canonical ticket format and the reusable
`ticket-creation` skill. It does not expand the lifecycle or add a second
checkpoint’s work. The purpose is to make ticket authoring consistent and
provider-neutral before narrower issue work depends on it.
