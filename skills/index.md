# AI Dev Skill Index

Use this catalog to determine whether a specialized AI Dev skill applies to the current task.

When a listed skill applies, load and follow its canonical `SKILL.md` before doing that work. Do not load unrelated skills.

This file is derivative of canonical skill metadata. `SKILL.md` files are the source of truth.

## Orchestration

### work-agent-orchestration

Use whenever helping the user direct, debug, or refine constrained work AIs, including deciding what instructions to relay, diagnosing work-agent failures, or tuning the CLAUDE.md instructions, permissions, and work-agent-specific skills used in that environment. Optimize for low manual relay cost and preserve the boundary between work-agent-specific constraints and globally useful guidance.

Canonical skill: `skills/orchestration/work-agent-orchestration/SKILL.md`

## Work Agent Skills

### write-low-reasoning-skills

Use when writing or refining a skill specifically for the constrained work-agent environment. These skills may deliberately use more explicit constraints to achieve reliable minimum behavior from the approved work agents. Do not load this for ordinary/shared skill writing.

Canonical skill: `skills/work-agent-skills/write-low-reasoning-skills/SKILL.md`

### documentation

Use whenever discussing, writing, reviewing, or revising project documentation for the constrained work-agent environment, especially Markdown under `docs/`. Do not use this for the repository's root `README.md`; use `project-readme` instead.

Canonical skill: `skills/work-agent-skills/documentation/SKILL.md`

### project-readme

Use whenever discussing, writing, reviewing, or revising the repository's root `README.md` for the constrained work-agent environment. Treat that README as the project's front door and routing page rather than as ordinary detailed documentation.

Canonical skill: `skills/work-agent-skills/project-readme/SKILL.md`

## Reviews

### frontend-design-review

Use when reviewing screenshots, implemented UI, mockups, or proposed front-end designs for unnecessary cognitive load, weak action hierarchy, redundant visual containment, or failure to prioritize the normal happy path.

Canonical skill: `skills/reviews/frontend-design-review/SKILL.md`
