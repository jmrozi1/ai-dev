# Editable Config Slice 1 (Issue #17)

## Scope

Slice 1 establishes a user-editable AI Dev configuration workflow that is IDE-independent.

Implemented in this slice:

- bare `config` command opens editable user config
- deterministic user config path resolution
- default YAML creation when missing
- existing-file preservation (no rewrite)
- editor selection precedence and safe command parsing
- path-only fallback when no editor can be launched
- focused Linux and mocked Windows test coverage

Not implemented in this slice:

- alias desired-state reconciliation
- bootstrap installers/manifests
- profile include management

## Config Path Resolution

The command uses the existing user path foundation:

- `AI_DEV_CONFIG` override is authoritative
- Linux/XDG behavior uses `XDG_CONFIG_HOME` when present
- Windows behavior uses `APPDATA` when present
- fallback remains platform-default user config directory

The resolved path is printed as an absolute path.

## Bare Config Command

Usage:

```text
ai-dev config
```

Behavior:

1. resolve user config path
2. create parent directory if needed
3. create documented default YAML only if file is missing
4. preserve existing file byte-for-byte
5. resolve editor using deterministic precedence
6. launch editor when possible
7. otherwise print path and manual-edit guidance

`get`, `set`, and `unset` remain unchanged.

## Default YAML

When missing, the command creates a documented YAML file with safe defaults:

- `ai.delivery: stdout`
- `ai.invocation: "Read and execute {task_file}"`
- `reports.presentation: path-only`
- `editor.command: null`
- `aliases: {}`

The file is valid for the current config loader and avoids machine-specific paths.

## Editor Selection

Current implementation uses collapsed precedence:

- `editor.command`
- `VISUAL`
- `EDITOR`
- platform default (`vi` on Unix, `notepad.exe` on Windows)
- path-only fallback

This is intentionally equivalent to:

- `editor.command -> VISUAL -> EDITOR -> platform default -> path-only`

## Parsing and Safety

Editor command parsing is shell-safe and platform-aware:

- parse with tokenization, not `shell=True`
- preserve ordinary command arguments (for example `code --wait`)
- append config path exactly once as final argument
- do not interpret shell operators

## Fallback and Exit Semantics

- if config path/create succeeds but no editor launches:
  - print warning/path/manual guidance
  - exit `0`
- if config path/create fails:
  - return error
  - exit `1`

Editor launch failure never rewrites the config file.

## Atomicity and Preservation

- creation uses exclusive file creation semantics
- write failures remove partial files
- existing files are left unchanged

## Boundaries

This slice does not change:

- `showreport` behavior
- review/summarize verification behavior
- commit/promotion behavior

Alias reconciliation and bootstrap installers remain checkpoint 2/3 work.
