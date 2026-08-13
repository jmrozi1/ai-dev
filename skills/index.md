# AI Dev Skill Catalog

This catalog is derivative only.

Canonical instructions always live in each skill's `SKILL.md`. Use this file only when a provider cannot natively inspect `~/.agents/skills` and needs a cheap way to decide whether one canonical AI Dev skill should be loaded.

Do not preload every skill from this catalog. Do not treat it as a router, dependency graph, or authoritative metadata source.

## Skills

| Skill | Use when | Canonical path |
| --- | --- | --- |
| `auto-review` | Lifecycle review orchestration; checkpoint/promotion stage selection; candidate review composition and applicability; promotion-pass recording and gate interaction. | `skills/auto-review/SKILL.md` |
| `flow` | Running or validating `flow-*` commands, workflow safety, checkpoints, diff modes, branch relation, or ticket lifecycle transitions. | `skills/flow/SKILL.md` |
| `frontend-design-review` | Reviewing screenshots, implemented UI, mockups, or proposed front-end designs for unnecessary cognitive load, weak action hierarchy, redundant visual containment, or failure to prioritize the normal happy path. | `skills/frontend-design-review/SKILL.md` |
| `requirements-driven-development` | Defining, refining, implementing, validating, or reviewing behavior that needs explicit requirements and objective evidence. | `skills/requirements-driven-development/SKILL.md` |
| `orchestrator` | Coordinating bounded development work through durable intent, scope, delegation, tasking-file state, and evidence-based decisions. | `skills/orchestrator/SKILL.md` |
| `executor` | Executing a bounded development assignment deeply and narrowly, continuing independent work, and returning concise evidence for durable tasking state. | `skills/executor/SKILL.md` |
| `review-process` | Atomic process-review judgment for checkpoint or promotion contexts; assess approach quality, intervention balance, wasted effort, and skill opportunities. | `skills/review-process/SKILL.md` |
| `work-agent-orchestration` | Directing, debugging, or refining constrained work agents and their reference instructions, permissions, and work-agent-specific skills. | `skills/work-agent-skills/work-agent-orchestration/SKILL.md` |
| `documentation` | Writing, reviewing, or revising project documentation for the constrained work-agent environment, excluding the root README. | `skills/work-agent-skills/documentation/SKILL.md` |
| `project-readme` | Writing, reviewing, or revising the root README as the constrained work-agent environment's front door and routing page. | `skills/work-agent-skills/project-readme/SKILL.md` |
| `write-low-reasoning-skills` | Writing or refining skills specifically for the constrained work-agent environment. | `skills/work-agent-skills/write-low-reasoning-skills/SKILL.md` |
