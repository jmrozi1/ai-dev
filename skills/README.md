# Skills Package

AI Dev skills are organized by operating audience only when audience-specific
instructions materially change behavior, permissions, recovery, or evidence.

Canonical locations:

- `skills/<skill-name>/SKILL.md` for genuinely shared operational skills;
- `skills/chatgpt/<skill-name>/SKILL.md` for ChatGPT-oriented skills;
- `skills/copilot/<skill-name>/SKILL.md` for Copilot-oriented skills;
- `skills/work/<skill-name>/SKILL.md` for constrained work-agent skills.

Not every capability exists for every audience. Duplicate capability
implementations are allowed conceptually when audiences need materially
different operational guidance, but this checkpoint introduces no duplicates.
Audience specialization is justified by behavioral differences, not merely by
different wording or examples.

The current shared skills are `frontend-design-review`,
`requirements-driven-development`, and `review-process`. The current
audience-specific skills are:

- ChatGPT: `auto-review`, `flow`, `orchestrator`;
- Copilot: `auto-review`, `executor`, `flow`;
- Work: `work-agent-orchestration`, `documentation`, `project-readme`, and
  `write-low-reasoning-skills`.

Flow remains one deterministic runtime in `ai_dev_flow` with one launcher set,
owned by the Copilot Flow package for repository execution. ChatGPT receives
lifecycle interpretation guidance, and Work receives no Flow package.

Skill discovery is explicit rather than recursive. The installer recognizes
root shared packages and direct packages under `chatgpt/`, `copilot/`, and
`work/`. Each installation selects one audience and includes the root shared
packages in that audience's flat installed destination. Duplicate capability
names are valid across source audiences but cannot collide within one selected
installation.

`skills/index.md` is a thin derivative catalog for providers without native
skill discovery. Canonical `SKILL.md` files remain authoritative. Do not add a
router, inheritance system, synchronization framework, or speculative category
tree.

Optional subdirectories such as `scripts/`, `src/`, `tests/`, and `references/`
are allowed only when real skill content requires them.
