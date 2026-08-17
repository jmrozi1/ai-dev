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

The installer safely removes only legacy `flow-*` launchers that its ownership
record can prove AI Dev managed. It does not install PATH commands. Use
`--home <path>` to clean another existing AI Dev installation safely.

## Workflow

Flow owns deterministic issue and patch lifecycle state, checkpoint commits,
and repository safety. Normal operation is through the installed Copilot Flow
skill package: it maps lifecycle intent to local helpers and renders `/status`
from the active ticket roadmap. Legacy `flow-*` and `flow-ticket-*` PATH
commands are retired; ticket creation and refinement belong to ChatGPT.

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
