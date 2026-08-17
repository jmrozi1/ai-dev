---
name: status
description: Show AI Dev active-ticket and project progress
---

# Copilot /status Command

Use the **Copilot Flow** skill to service this command.

## Contract

- bare /status means the normal AI Dev ticket/project status interaction; no argument means normal AI Dev ticket/project status, not missing information.
- The bare /status path must not trigger a generic repository or Git status fallback.
- `/status verbose` means the existing verbose ticket-status interaction and must pass the `verbose` qualifier to the Copilot Flow status helper.
- Always delegate to the existing Copilot Flow status helper and return the existing helper output. Do not summarize a different status source.
- Do not run git status, do not synthesize repository status first, and do not ask what kind of status the user means.
- Do not show session history or standup status output, and do not replace the Flow result with generic workspace status.

## Intent Mapping

- bare /status → use the Copilot Flow skill’s normal ticket-status interaction (`scripts/ticket-status`)
- `/status verbose` → use the Copilot Flow skill’s verbose ticket-status interaction (`scripts/ticket-status verbose`)

## Behavior

Route the user's `/status` request to the Copilot Flow skill, which owns the active-ticket and project-progress status interaction and delegates to the canonical `ai_dev_flow.ticket_status` module.

Do **not**:

- Do not run git status or repository diagnostics as a substitute
- Treat bare /status as missing input or ask for clarification
- Do not show session history or standup status output
- Implement custom status rendering or summarize a different status source

## Verbose Argument

When the user provides `/status verbose`, pass the `verbose` intent to the Copilot Flow skill. That path includes the full ticket description, detailed roadmap, and expanded status details from the existing helper output.

---

**Skill Reference**: [Copilot Flow](../../skills/copilot/flow/SKILL.md)
