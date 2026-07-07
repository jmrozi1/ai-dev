# AI Dev Core

Shared AI development workflow prompts, templates, profiles, conventions, and schemas.

The initial focus is documentation generation and documentation review, but the long-term goal is to support broader AI-assisted development workflows such as status, planning, code review, test review, requirement validation, and scope control.

## MVP Focus

The first MVP supports:

- Generate → Documentation
- Review → Documentation

The generated documentation is primarily an AI navigation layer. It should help AI answer questions such as:

- Where do I go to resolve X?
- What file handles this behavior?
- What job do I run to install Kubernetes on my box?
- What parameters are required for this job?
- What happens after this job runs?

## Repository Structure

```text
ai-dev-core/
  workflows/
    generate-docs/
    review/
    status/
    plan/
  profiles/
  conventions/
  schemas/
```

## Design Principles
- Source is truth.
- Documentation is an AI navigation layer.
- Documentation is reviewable.
- Default to changed files.
- Bootstrap mode is explicit.
- Unit docs come before indexes and READMEs.
- Hashes are preferred over timestamps.
- IDE wrappers should stay thin.