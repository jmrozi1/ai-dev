# AI Dev VS Code

AI Dev VS Code is the editor wrapper for AI Dev workflows.

The extension helps generate, review, and use AI navigation documentation directly from VS Code. It is intentionally thin: reusable workflow instructions, templates, profiles, conventions, and schemas live in `ai-dev-core`.

## Features

- Adds an **AI Dev** activity bar view.
- Adds AI Dev commands to explorer, editor, and source control menus.
- Generates summary documentation for selected files and folders.
- Reviews documentation against changed source files.
- Answers questions from generated AI navigation docs.
- Supports prompt-only and direct experimental workflow modes.
- Uses bundled `ai-dev-core` workflow files when no workspace-specific `aiDevCore.path` is configured.

## Requirements

A workspace should include a `.ai-dev.yaml` file. The extension uses that file for documentation settings, source exclusions, and AI provider mode.

A minimal workspace config looks like this:

```yaml
documentation:
  docsDir: ai-docs

aiProvider:
  mode: direct-experimental
```

`aiDevCore.path` is optional for packaged VSIX installs. When omitted, the extension uses the bundled `vendor/ai-dev-core` copy included in the VSIX.

## Build

From the combined repository root:

```bash
./scripts/build-vsix.sh
```

The final installable package is written to the top-level `artifacts/` directory.

## Install

```bash
code --install-extension artifacts/ai-dev-vscode-0.0.1.vsix --force
```

## Notes

This extension is currently an MVP artifact. The workflow model is still evolving, but the packaging goal is simple: install the VS Code wrapper as a VSIX instead of launching it through an extension development host.
