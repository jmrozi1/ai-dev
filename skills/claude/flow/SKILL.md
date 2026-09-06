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
scope, and the authorized rail. Follow only that rail.

Unnamed, discovery derives everything from the workspace and authorizes the one
`ready` rail in the scope. When you were launched onto a named rail, or you are
reviewing from a workspace that holds no claim, name what you were given:

```bash
ai-dev discover --rail <rail-id> --project <project> --ticket issue-<n>
```

Naming does not weaken authorization; it says which rail and scope to read.
Details are in Naming an Assigned Rail below.

## Discovery Contract

| Need | Source |
| --- | --- |
| Repository identity | `git remote get-url origin`, normalized to `owner/repo` |
| Project namespace | the repository name, or an explicitly named `--project` |
| Ticket | `activeIssueNumber` in Flow workflow state, as `issue-<n>`, or an explicitly named `--ticket issue-<n>` |
| Workspace | the Issue #50 claim registry |
| Rail | unnamed: the single `ready` rail in the control-plane scope. Named with `--rail`: exactly that rail, resolvable at `ready` or `running` |
| Executing runtime | the AI Dev checkout owning the running module, and its revision |
| Routing instructions | this skill's file and revision in that runtime |

`ai-dev identity` resolves identity alone, which is useful when control-plane
data is unavailable or malformed and you need to prove what the repository is.

Discovery and status report the source of every value they print. Report those
sources as they were reported to you. Do not restate a value without its source,
and do not carry a value forward from an earlier session in place of reading it.
A value you named is reported as explicitly named, so a named scope is never
mistaken for one a reader derived.

### Naming an Assigned Rail

Two legitimate situations cannot be served by derivation alone.

- **You were launched onto a rail.** The orchestrator moves a rail it launches
  from `ready` to `running`, so the rail you are executing is exactly the one
  the unnamed path refuses. Name it.
- **You are reviewing from a claimless workspace.** An independent reviewer
  works in a disposable clone with no Flow workflow by contract. Starting one so
  discovery can read `activeIssueNumber` would acquire a claim and break that
  contract. Name the scope instead.

What naming does and does not change:

- `--rail` resolves exactly the rail you named, and only at `ready` or
  `running`. It never falls back to another rail, and a rail that is `blocked`
  or `completed` is refused: a stopped rail is not an instruction to execute and
  a completed rail is already accepted. If the rail you were given is closed,
  report that and stop -- do not name a different one.
- `--project` and `--ticket` supply the control-plane scope the rail lives in.
  They replace derivation, not proof: Git repository identity is still read from
  the workspace, and an unusable project or a ticket that is not `issue-<n>`
  fails closed.
- Naming adds no write and acquires no claim. `ai-dev discover` is read-only
  whether or not you name anything.
- Naming nothing behaves exactly as it always has.

Name only what durable state gave you -- your rail authorization, or the scope
your assignment identified. A rail id you remember, inferred, or preferred is
not authorization.

### Coordination Repository Reconciliation

Two things can name the coordination repository: this workspace's clone-local
`.ai-dev/config.json` `controlPlane.repository`, and the managed host cache.
Discovery reconciles them by one rule -- they agree when they identify the same
coordination repository -- and reports which one served the read:

- only the managed cache is present: it is used;
- workspace configuration is present and the managed cache is absent: the
  configured repository is used, and rails are read from it;
- both are present and identify the same repository: reconciled, and the managed
  cache serves the read;
- both are present and identify different repositories: discovery stops with
  both identities.

A disagreement is a decision about which coordination repository is correct.
Do not choose one, edit configuration to silence it, or read rails from the
other. Report both identities and stop.

## Fail Closed

Discovery stops with an actionable diagnostic and no speculative execution when
repository identity is unresolvable, no Flow ticket is active, no coordination
repository can be resolved from either workspace configuration or the managed
cache, the two identify different coordination repositories, the configured
coordination repository is unusable, the executing runtime has no Claude Flow
skill to route through, the project/ticket namespace does not exist, or -- when
no rail is named -- zero or more than one rail is ready.

Naming keeps every one of those, and adds its own: a named project that is not a
usable identifier, a named ticket that is not `issue-<n>`, a named rail that
does not exist in the scope, and a named rail whose durable status is anything
other than `ready` or `running`.

When it stops, report exactly what it reported. Never substitute Claude memory,
product documentation, an issue comment, a product-local handoff file,
`.ai-dev/tasking.md`, or a Flow checkpoint number for durable authorization.

## Lifecycle Routing

Lifecycle commands remain the shared Flow runtime's. Route intents to the
installed Flow helpers rather than reimplementing them, and read the shared
`executor` skill for the role contract, publication rules, and stop conditions.

These are the Claude-audience routes. They cover the capabilities a Claude rail
needs end to end, so a Claude executor never needs the Copilot audience's
instructions to satisfy its own preconditions.

| Intent | Route |
| --- | --- |
| Resolve the authorized rail | `ai-dev discover` (add `--json` for machine-readable provenance) |
| Resolve a rail you were explicitly assigned | `ai-dev discover --rail <rail-id>`, adding `--project <project> --ticket issue-<n>` when the workspace has no active Flow workflow |
| Prove repository/ticket identity alone | `ai-dev identity` |
| Inspect workspace, claim, runtime, skill, and control-plane provenance | `ai-dev status` |
| Gather checkpoint or promotion review evidence | `ai-dev review-evidence --mode checkpoint\|promotion` |
| Publish the executor handoff and take a receipt | `ai-dev publish --file <handoff> --rail <rail-id>` |
| Locate or refresh the managed coordination cache | `ai-dev cache-path`, `ai-dev cache-sync` |
| Run a Flow lifecycle command | the installed `flow-*` launchers (`flow-status`, `flow-commit`, ...) |

`discover`, `identity`, `status`, `review-evidence`, and `cache-path` are
read-only: they resolve and report, and never acquire a claim or write
coordination state. `publish`, `cache-sync`, and the `flow-*` lifecycle commands
mutate; run them only when your rail authorizes that step.

Claim evidence comes from `ai-dev status`, which reads the Issue #50 claim
registry without acquiring anything. A malformed claim is reported as malformed;
treat it as occupied, never as absent.
