# AI Dev Skill Catalog

This catalog is a lightweight discovery surface and derivative catalog.

Canonical instructions always live in each skill's `SKILL.md`. Use this file
when a provider cannot natively inspect `~/.agents/skills` or a project's
repository-local skill directory and needs a lightweight way to evaluate the
full current task against the available catalog.

Do not preload every skill from this catalog. Do not treat it as a router, dependency graph, or authoritative metadata source.

Evaluate the full current development task and determine which skill or skills
materially apply. Do not stop after finding the first match. Compose multiple
skills when they own distinct responsibilities required by the task, but do not
load skills merely because they are adjacent or potentially useful. Composition
does not imply a dependency graph, router, prefix convention, or automatic
recursive loading.

## ChatGPT skill observability

When ChatGPT uses this catalog to choose an AI Dev skill, the user-facing
response must make the selection result observable.

- If one skill is selected, follow that skill's ChatGPT interaction instructions.
- If multiple skills are selected, list every materially active skill in a
	responsibility-ordered chain and follow each skill's ChatGPT interaction
	instructions.
- If no AI Dev skill applies, begin the response with:

	`Skill: none`

This convention is ChatGPT-facing only. It does not change Copilot behavior.
The catalog owns only this observable fallback; review and design reasoning rules
remain in the applicable canonical `SKILL.md` files.

## Shared skills

| Skill | Use when | Canonical path |
| --- | --- | --- |
| `executor` | Executing a bounded development assignment deeply and narrowly, continuing independent work, and returning concise evidence for durable tasking state. | `skills/executor/SKILL.md` |
| `feedback-loop-design` | Designing discovery, prototyping, implementation, or validation loops when builds, live environments, screenshots, human relay, or other feedback are materially slow or costly. | `skills/feedback-loop-design/SKILL.md` |
| `investigation-synthesis` | Preserving the durable results of substantial research, debugging, reverse engineering, or source comparison without turning the investigation into transcript documentation; route accepted findings to the smallest correct durable home and discard the rest. | `skills/investigation-synthesis/SKILL.md` |
| `frontend-design-review` | Reviewing screenshots, implemented UI, mockups, or proposed front-end designs for unnecessary cognitive load, weak action hierarchy, redundant visual containment, or failure to prioritize the normal happy path. | `skills/frontend-design-review/SKILL.md` |
| `requirements-driven-development` | Defining, refining, implementing, validating, or reviewing behavior that needs explicit requirements and objective evidence. | `skills/requirements-driven-development/SKILL.md` |
| `review-process` | Atomic checkpoint/promotion process review; assess approach, intervention balance, wasted effort, and maintain/reassess ticket `Skill Candidates` through final disposition and accepted skill remediation. | `skills/review-process/SKILL.md` |

## ChatGPT

| Skill | Use when | Canonical path |
| --- | --- | --- |
| `auto-review` | Deciding checkpoint/promotion review composition and pass/action-required judgment, including final readiness of ticket skill-candidate and accepted-skill state; also deciding whether in-flight process review applies at a natural orchestrator handoff while a named checkpoint is still in progress. | `skills/chatgpt/auto-review/SKILL.md` |
| `flow` | Interpreting Flow lifecycle state, deciding valid transitions, and escalating blocked or unsafe workflow state. | `skills/chatgpt/flow/SKILL.md` |
| `orchestrator` | Coordinating bounded development work through durable intent, scope, delegation, tasking-file state, ticket-owned skill investment state, and evidence-based decisions. | `skills/chatgpt/orchestrator/SKILL.md` |
| `skill-authoring` | Creating/refining AI Dev skills or adjudicating ticket skill candidates as keep/no-skill/refine/create, with immediate dogfood for accepted investments. | `skills/chatgpt/skill-authoring/SKILL.md` |
| `ticket-creation` | Creating task records using the canonical ticket schema, including `Skill Candidates` and `Skills` accounting sections, and repository ticket-provider contract. | `skills/chatgpt/ticket-creation/SKILL.md` |
| `work-skill-refinement` | Designing and refining work-side AI skills and capability boundaries from observed work-agent behavior, including deciding when a task should not be delegated to the work AI. | `skills/chatgpt/work-skill-refinement/SKILL.md` |

## Claude

| Skill | Use when | Canonical path |
| --- | --- | --- |
| `auto-review` | Routing Claude checkpoint and promotion review to the shared deterministic review-evidence and recording helpers without duplicating review judgment. | `skills/claude/auto-review/SKILL.md` |
| `flow` | Resolving AI Dev repository, ticket, workspace, and control-plane rail identity from a Claude session, and routing lifecycle intents to the shared deterministic Flow runtime. | `skills/claude/flow/SKILL.md` |

## Copilot

| Skill | Use when | Canonical path |
| --- | --- | --- |
| `auto-review` | Gathering repository review evidence, including active ticket `Skill Candidates`/`Skills`, running deterministic helpers, and recording only when explicitly authorized. | `skills/copilot/auto-review/SKILL.md` |
| `flow` | Executing and validating Flow lifecycle commands, preconditions, checkpoint mechanics, promotion/retry/completion safety, and evidence reporting. | `skills/copilot/flow/SKILL.md` |

## Project-local skills

Project-local skills remain canonical in their code repository; they are not
installed or owned by AI Dev. When the current task concerns a project listed
below, evaluate its local skills alongside the shared/audience skills above.
Fetch the project's canonical `SKILL.md` when accessible before relying on the
entry. If the canonical file is not yet available, treat this section only as a
discovery hint rather than a substitute instruction set. Do not activate these
skills for unrelated repositories.

### `family-dragonflight-server`

Code repository: `jeffmrozinski-cell/family-dragonflight-server`

| Skill | Use when | Canonical project path |
| --- | --- | --- |
| `manage-wow-servers` | Starting, reusing, checking readiness of, or stopping the project-owned `bnetserver` / `worldserver` processes with exact ownership and fail-closed lifecycle behavior. | `.agents/skills/manage-wow-servers/SKILL.md` |
| `start-wow-client` | Starting or reusing the canonical Firestorm client with exact process/path ownership evidence; not for credentials, login, character selection, gameplay input, or broad client termination. | `.agents/skills/start-wow-client/SKILL.md` |
| `run-wow-tests` | Selecting/running project tests, especially the explicitly authorized bounded live WowTest lifecycle; prefer non-live tests by default and compose with the server/client skills only when the requested evidence requires them. | `.agents/skills/run-wow-tests/SKILL.md` |

Audience sections are a catalog, not a router. Not every capability exists for
every audience. Duplicate capability names are permitted by the architecture
when material behavioral differences justify them. Install one selected
audience at a time; root shared skills are included with that selection, and
there is no flat all-audience installation mode.
