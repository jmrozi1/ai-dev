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

## Design Principles

- Source is truth.
- Documentation is an AI navigation layer.
- Documentation should be reviewable.
- IDE wrappers should stay thin.
- The VSIX should be installable without launching the extension from source.
