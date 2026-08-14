# AI Dev Skill Catalog

This catalog is derivative only.

Canonical instructions always live in each skill's `SKILL.md`. Use this file only when a provider cannot natively inspect `~/.agents/skills` and needs a cheap way to decide whether one canonical AI Dev skill should be loaded.

Do not preload every skill from this catalog. Do not treat it as a router, dependency graph, or authoritative metadata source.

## Shared skills

| Skill | Use when | Canonical path |
| --- | --- | --- |
| `frontend-design-review` | Reviewing screenshots, implemented UI, mockups, or proposed front-end designs for unnecessary cognitive load, weak action hierarchy, redundant visual containment, or failure to prioritize the normal happy path. | `skills/frontend-design-review/SKILL.md` |
| `requirements-driven-development` | Defining, refining, implementing, validating, or reviewing behavior that needs explicit requirements and objective evidence. | `skills/requirements-driven-development/SKILL.md` |
| `review-process` | Atomic process-review judgment for checkpoint or promotion contexts; assess approach quality, intervention balance, wasted effort, and skill opportunities. | `skills/review-process/SKILL.md` |

## ChatGPT

| Skill | Use when | Canonical path |
| --- | --- | --- |
| `auto-review` | Deciding review applicability and composition, synthesizing findings, judging materiality, and authorizing promotion-review recording. | `skills/chatgpt/auto-review/SKILL.md` |
| `flow` | Interpreting Flow lifecycle state, deciding valid transitions, and escalating blocked or unsafe workflow state. | `skills/chatgpt/flow/SKILL.md` |
| `orchestrator` | Coordinating bounded development work through durable intent, scope, delegation, tasking-file state, and evidence-based decisions. | `skills/chatgpt/orchestrator/SKILL.md` |

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
