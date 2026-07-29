# Generated-Task Foundation (Issue #14)

## Scope

Issue #14 establishes the script-level foundation for generated tasks, machine-local configuration, delivery adapters, editor opening, and report presentation.

This issue does not migrate summarize/review command behavior.

Native verification commitment for issue #14 is Linux plus pending native Windows checks.
macOS behavior remains best-effort and currently unverified natively for issue closure.

## Generated Task Artifacts

Task preparation writes two Markdown artifacts:

- `.ai-dev/tasks/<task-id>.md` (immutable per task id)
- `.ai-dev/current-task.md` (current pointer)

Task-id grammar:

- starts with letter or digit
- may include letters, digits, `.`, `_`, `-`
- must not be `.` or `..`
- max length: 128 characters

Write semantics:

- task file is written atomically
- current-task pointer is written atomically
- if pointer write fails after task creation, task file is rolled back

Task file format is canonical Markdown with sections:

- `# AI Dev Generated Task: <task-id>`
- `## Metadata`
- `## Task`
- `## Constraints`
- `## Expected Output`

## User Configuration

User configuration file path:

- Linux:
  - `$AI_DEV_CONFIG` when set
  - otherwise `$XDG_CONFIG_HOME/ai-dev/config.yaml` when `XDG_CONFIG_HOME` is set
  - otherwise `~/.config/ai-dev/config.yaml`
- macOS:
  - `$AI_DEV_CONFIG` when set
  - otherwise `$XDG_CONFIG_HOME/ai-dev/config.yaml` when `XDG_CONFIG_HOME` is set
  - otherwise `~/.config/ai-dev/config.yaml`
- Windows:
  - `%AI_DEV_CONFIG%` when set
  - otherwise `%APPDATA%\\ai-dev\\config.yaml` when `APPDATA` is set
  - otherwise `%USERPROFILE%\\AppData\\Roaming\\ai-dev\\config.yaml`

Config is strict YAML with field-path and file-path validation errors.

Machine-owned settings are user-config only:

- `ai.delivery`
- `ai.invocation`
- `editor.command`
- `reports.presentation`

Repository `.ai-dev.yaml` is read for compatibility but cannot override those machine preferences.

Example valid user config:

```yaml
ai:
  delivery: clipboard+stdout
  invocation: "Read and execute {task_file}"

editor:
  command: "code --wait"

reports:
  presentation: editor
```

`flow config` behavior:

- creates the user config file only when missing
- preserves existing file contents when present
- if config is malformed or invalid, still opens that exact file for repair using fallback editor resolution

## Invocation and Delivery

Invocation template variables:

- `{task_file}`
- `{task_id}`
- `{task_type}`

Malformed templates fail with actionable config errors.

Supported delivery modes:

- `stdout`
- `file-only`
- `clipboard`
- `clipboard+stdout`

Clipboard adapter behavior:

- Linux command candidates: `wl-copy`, `xclip -selection clipboard`, `xsel --clipboard --input`
- macOS command: `pbcopy` (implemented, best-effort, not currently in the native verification gate for issue #14)
- Windows command candidates:
  - `powershell -NoProfile -NonInteractive -Command "[Console]::In.ReadToEnd() | Set-Clipboard"`
  - `powershell.exe -NoProfile -NonInteractive -Command "[Console]::In.ReadToEnd() | Set-Clipboard"`
- invocation text is passed via stdin to clipboard command
- shell interpolation is not used

Clipboard fallback semantics:

- `clipboard`: warning to stderr, invocation fallback to stdout, command still succeeds
- `clipboard+stdout`: invocation is already on stdout; clipboard failure is warning-only

Generated task artifacts are preserved when clipboard delivery fails.

## Editor Behavior

Editor command precedence:

1. `editor.command`
2. `VISUAL`
3. `EDITOR`
4. platform fallback (`notepad` on Windows, `vi` otherwise)
5. path fallback (if no launchable editor)

Fallback warnings are preserved and surfaced even when opening succeeds.

Runtime behavior:

- terminal editors (for example `vi`, `vim`, `nvim`, `nano`, `emacs`) inherit stdio and block until exit
- non-terminal editors launch detached from stdio and do not block indefinitely
- explicit wait editors use bounded wait semantics
- target file path is appended as a separate argv argument
- shell interpolation is not used

When no editor can be launched, commands print the file path so users can open it manually.

## Report Presentation

`flow showreport` resolves report path in this order:

- repository configured output path (`.ai-dev/config.json` `out`)
- default `out.txt` in repository root

Supported presentation modes (`reports.presentation`):

- `stdout`
- `editor`
- `path-only`

Behavior requirements:

- canonical report stays Markdown on disk
- report content is not duplicated
- editor failure still surfaces report path
- missing/unreadable reports fail cleanly with warning and path output

## Issue Boundaries

- Issue #14: foundations only (this document)
- Issue #15: summarize migration
- Issue #16: review migration
- Issue #17: bootstrap/config-editing/alias usability expansion
- Native completion gate for issue #14: Windows verification only; macOS is out of the current verification commitment.
