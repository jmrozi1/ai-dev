# AI Dev Skill Catalog

This catalog is a lightweight discovery surface and derivative catalog.

Canonical instructions always live in each skill's `SKILL.md`. Use this file
when a provider cannot natively inspect `~/.agents/skills` and needs a lightweight
way to evaluate the full current task against the catalog.

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

This convention is ChatGPT-facing only. It does not change Copilot or Work
behavior. The catalog owns only this observable fallback; review and design
reasoning rules remain in the applicable canonical `SKILL.md` files.

## Shared skills

| Skill | Use when | Canonical path |
| --- | --- | --- |
| `frontend-design-review` | Reviewing screenshots, implemented UI, mockups, or proposed front-end designs for unnecessary cognitive load, weak action hierarchy, redundant visual containment, or failure to prioritize the normal happy path. | `skills/frontend-design-review/SKILL.md` |
| `requirements-driven-development` | Defining, refining, implementing, validating, or reviewing behavior that needs explicit requirements and objective evidence. | `skills/requirements-driven-development/SKILL.md` |
| `review-process` | Atomic process-review judgment for checkpoint or promotion contexts; assess approach quality, intervention balance, wasted effort, and skill opportunities. | `skills/review-process/SKILL.md` |
| `search-select` | Designing, implementing, or reviewing a browse-first searchable single-select control with ordinary-selection or select-to-add behavior. | `skills/search-select/SKILL.md` |

## ChatGPT

| Skill | Use when | Canonical path |
| --- | --- | --- |
| `auto-review` | Deciding review applicability and composition, synthesizing findings, judging materiality, and authorizing promotion-review recording. | `skills/chatgpt/auto-review/SKILL.md` |
| `flow` | Interpreting Flow lifecycle state, deciding valid transitions, and escalating blocked or unsafe workflow state. | `skills/chatgpt/flow/SKILL.md` |
| `frontend-design` | Designing or materially restructuring front-end screens or interaction flows before implementation; use cheap ASCII/text prototypes when structural decisions remain unresolved. | `skills/chatgpt/frontend-design/SKILL.md` |
| `orchestrator` | Coordinating bounded development work through durable intent, scope, delegation, tasking-file state, and evidence-based decisions. | `skills/chatgpt/orchestrator/SKILL.md` |
| `skill-authoring` | Creating or refining an AI Dev skill, or performing focused skill-quality review when execution evidence suggests a skill-definition problem. | `skills/chatgpt/skill-authoring/SKILL.md` |
| `ticket-creation` | Creating task records using the canonical ticket schema and repository ticket-provider contract. | `skills/chatgpt/ticket-creation/SKILL.md` |

## Copilot

| Skill | Use when | Canonical path |
| --- | --- | --- |
| `auto-review` | Gathering repository review evidence, running deterministic helpers, executing approved review mechanics, and recording only when explicitly authorized. | `skills/copilot/auto-review/SKILL.md` |
| `executor` | Executing a bounded development assignment deeply and narrowly, continuing independent work, and returning concise evidence for durable tasking state. | `skills/copilot/executor/SKILL.md` |
| `flow` | Executing and validating Flow lifecycle commands, preconditions, checkpoint mechanics, promotion/retry/completion safety, and evidence reporting. | `skills/copilot/flow/SKILL.md` |

## Work

| Skill | Use when | Canonical path |
| --- | --- | --- |
| `work-agent-orchestration` | Directing, debugging, or refining constrained work agents and their reference instructions, permissions, and work-agent-specific skills. | `skills/work/work-agent-orchestration/SKILL.md` |
| `documentation` | Writing, reviewing, or revising project documentation for the constrained work-agent environment, excluding the root README. | `skills/work/documentation/SKILL.md` |
| `project-readme` | Writing, reviewing, or revising the root README as the constrained work-agent environment's front door and routing page. | `skills/work/project-readme/SKILL.md` |
| `write-low-reasoning-skills` | Writing or refining skills specifically for the constrained work-agent environment. | `skills/work/write-low-reasoning-skills/SKILL.md` |

Audience sections are a catalog, not a router. Not every capability exists for
every audience. Duplicate capability names are permitted by the architecture
when material behavioral differences justify them. Install one selected
audience at a time; root shared skills are included with that selection, and
there is no flat all-audience installation mode.
