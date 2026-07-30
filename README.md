# AI Dev

AI Dev is a VS Code-assisted workflow system for generating, reviewing, and using AI navigation documentation.

This repository combines:

- `ai-dev-core` — shared workflows, templates, profiles, conventions, and schemas.
- `ai-dev-vscode` — thin VS Code wrapper that exposes the workflows in the editor.
- `scripts` — repository-level build and packaging scripts.
- `artifacts` — generated deliverables such as installable `.vsix` files.

## Build VSIX

```bash
./scripts/build-vsix.sh
```

The build script vendors `ai-dev-core` into the VS Code extension package and writes the final `.vsix` into `artifacts/`.

## Install VSIX

```bash
code --install-extension artifacts/*.vsix --force
```

## Install Canonical ai-dev Command

Linux and macOS:

```bash
./scripts/bootstrap-ai-dev.sh
```

Windows PowerShell:

```powershell
.\scripts\bootstrap-ai-dev.ps1
```

Both wrappers delegate to `python -m ai_dev_flow.bootstrap`, which installs managed `ai-dev` launcher files under `~/.local/bin` and prints PATH guidance when needed.

## Design Principles

- Source is truth.
- Documentation is an AI navigation layer.
- Documentation should be reviewable.
- IDE wrappers should stay thin.
- The VSIX should be installable without launching the extension from source.

## Generated-Task Foundation

- [docs/generated-task-foundation.md](docs/generated-task-foundation.md) - Issue #14 foundation behavior, configuration, delivery, editor, and report presentation.
- [docs/native-platform-verification-checklist.md](docs/native-platform-verification-checklist.md) - Native Windows verification checklist and block-reason text for Issue #14.
- [docs/summarize-planning-slice1.md](docs/summarize-planning-slice1.md) - Issue #15 Slice 1 deterministic summarize planning scope and behavior.
- [docs/summarize-task-preparation-slice2.md](docs/summarize-task-preparation-slice2.md) - Issue #15 Slice 2 deterministic summarize task preparation, batching, and delivery behavior.
- [docs/summarize-verification-slice3.md](docs/summarize-verification-slice3.md) - Issue #15 Slice 3 deterministic summarize post-execution verification behavior and reporting.
- [docs/review-package-slice1.md](docs/review-package-slice1.md) - Issue #16 Slice 1 deterministic review planning/package foundation and immutable artifacts.
- [docs/review-task-preparation-slice2.md](docs/review-task-preparation-slice2.md) - Issue #16 Slice 2 generated review task preparation, provider-neutral delivery, and current-task pointer integration.
- [docs/review-verification-slice3.md](docs/review-verification-slice3.md) - Issue #16 Slice 3 deterministic review report validation, package/task integrity checks, and review verification presentation behavior.
- [docs/editable-config-slice1.md](docs/editable-config-slice1.md) - Issue #17 Slice 1 user-editable config creation/open flow, editor precedence, and path-only fallback behavior.
- [docs/managed-aliases-slice2.md](docs/managed-aliases-slice2.md) - Issue #17 Slice 2 managed command alias reconciliation, profile ownership, and manifest/rollback behavior.
- [docs/bootstrap-slice3.md](docs/bootstrap-slice3.md) - Issue #17 Slice 3 cross-platform bootstrap and canonical ai-dev launcher installation behavior.
