# AI Provider Configuration

## Purpose

Define how AI Dev workflow prompt-delivery behavior is configured for the canonical `ai-dev` CLI.

Projects configure provider behavior in `.ai-dev.yaml` under `aiProvider`. The provider layer is intentionally vendor-neutral.

## Current Mode

### `prompt-only`

Commands prepare workflow prompts but do not execute an AI model.

```yaml
aiProvider:
  mode: "prompt-only"
```

Use this mode when AI execution must remain manual or externally controlled.

## Operational Notes

- Runtime summarize/review/task-prepare/showreport entry points were retired; use repository instructions/skills for summarization and review guidance. Current CLI entry points include `ai-dev flow ...`, `ai-dev config`, and `ai-dev apply`.
- AI Dev keeps source files and deterministic validation artifacts as the authority.
- No VS Code extension is required for supported workflow execution.

## Security and Change-Control Guidance

- Review repository configuration before running generated tasks.
- Prefer `prompt-only` in restricted or shared environments.
- Keep execution and credentials policies in your approved CLI/tooling layer.

## Relation to Other Rules

- [git-integration.md](git-integration.md) - Git diff as the safety gate for generated file changes.
- [documentation-layout.md](documentation-layout.md) - Where generated files are written.
