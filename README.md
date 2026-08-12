# AI Dev

AI Dev is a repository-local system for using AI skills and deterministic
workflow tools during software development. Its reusable instructions live in
the `skills/` package; its shared documentation, workflows, profiles, and
schemas live in `ai-dev-core/`.

## Install

On Linux or macOS:

```bash
./scripts/install.sh
```

On Windows PowerShell:

```powershell
.\scripts\install.ps1
```

The installer installs the fixed `flow-*` launchers. Use `--verbose` (or `-v`)
for a detailed bootstrap report and `--force` when noninteractive replacement
of conflicting managed launchers is required.

## Workflow

Flow owns issue and patch lifecycle state, checkpoint progression, ticket
operations, and read-only diff inspection. The supported top-level executables
are:

```text
flow-start
flow-patch
flow-status
flow-diff
flow-commit
flow-reset
flow-promote
flow-complete
flow-block
flow-resume
flow-ticket-create
flow-ticket-show
flow-ticket-query
```

A typical issue workflow is:

```text
flow-start <issue-number>
# implement and test a bounded slice
flow-diff --git
flow-commit
flow-promote "Describe the completed change"
flow-complete
```

Use `flow-patch "<description>"` for a small local change, or
`flow-patch --adopt "<description>"` when suitable work already exists on
`scratch`. Use `flow-status -v` to inspect active workflow state. `flow-diff`
and its `--refresh`, `--git`, and `--all` modes are read-only; use
`flow-diff --help` for their exact scope.

Ticket commands use the configured provider in `.ai-dev/config.json`. The
configuration is for ticket access, not repository output routing.

## Skills

Canonical skills live at `skills/<skill-name>/SKILL.md`. Providers with native
skill discovery should use their native mechanism. Providers without native
discovery can inspect [`skills/index.md`](skills/index.md) when a task may match
a reusable skill, then load only the clearly relevant canonical `SKILL.md`
files. Canonical skill files are authoritative over the catalog.

If the catalog or a selected skill cannot be retrieved, report that failure
instead of claiming the skill was loaded. Provider-specific startup
instructions should remain a thin wrapper around this discovery contract.

For a non-native ChatGPT-style provider, use this startup instruction:

When a task may match an AI Dev skill, inspect `skills/index.md`. Load only
clearly relevant canonical `skills/<skill-name>/SKILL.md` files. Canonical
`SKILL.md` instructions override the catalog. If the catalog or a selected
skill cannot be retrieved, report that failure instead of pretending it was
loaded. For development work, default to the `orchestrator` skill unless the
user explicitly assigns another role. Do not preload all skills.

Issue #30 defines the canonical orchestrator and executor skills. Until the
orchestrator skill exists, treat this as the intended provider default rather
than duplicating orchestrator behavior in bootstrap instructions.

For development work, a provider may select the future orchestrator role skill
by default; the orchestrator and executor role definitions belong to Issue #30.
This repository does not add role metadata to capability skills or the catalog.

See [`skills/README.md`](skills/README.md) for the package layout and rules.

## Tests

Run the default Python unit suite with:

```bash
./scripts/test.sh
```

List available suites with `./scripts/test.sh --list`. The `bootstrap`,
`flow`, `integration`, and `all` suites provide broader coverage.
