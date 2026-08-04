# AI Dev

AI Dev is a CLI-first workflow system for generating, reviewing, and using AI navigation documentation.

This repository combines:

- `ai-dev-core` — shared workflows, templates, profiles, conventions, and schemas.
- `scripts` — repository-level launcher, bootstrap, and validation scripts.

## Public Script Entry Points

Install:

```bash
./scripts/install.sh
```

Windows PowerShell:

```powershell
.\scripts\install.ps1
```

Test entry points:

```bash
./scripts/test.sh
```

```powershell
.\scripts\test.ps1
```

Canonical test suites:

- `unit` (default): fast Python unit coverage
- `bootstrap`: bootstrap-focused Python tests + shell bootstrap suites
- `flow`: shell lifecycle suites discovered under `tests/shell/flow/`
- `integration`: broader cross-component Python discovery
- `all`: complete Python + shell matrix

Examples:

```bash
./scripts/test.sh
./scripts/test.sh --list
./scripts/test.sh flow
./scripts/test.sh integration -- -k review
./scripts/test.sh all
```

```powershell
.\scripts\test.ps1
.\scripts\test.ps1 --list
.\scripts\test.ps1 flow
.\scripts\test.ps1 integration -- -k review
.\scripts\test.ps1 all
```

On PowerShell, shell suites are executed through `bash` when available. If `bash`
is unavailable, shell suites are explicitly reported as skipped.

## Legacy Compatibility Install Wrappers

Compatibility wrappers (retained during migration):

```bash
./tools/compatibility/bootstrap-ai-dev.sh
```

Windows PowerShell:

```powershell
.\tools\compatibility\bootstrap-ai-dev.ps1
```

Both wrappers are deprecated compatibility entry points. They print migration guidance and delegate to `scripts/install.sh` / `scripts/install.ps1`.

Primary workflows are CLI-first:

```text
ai-dev flow start ...
ai-dev flow patch ...
ai-dev flow task-prepare ...
ai-dev flow status ...
ai-dev flow review ...
ai-dev flow commit ...
ai-dev flow reset ...
ai-dev flow promote ...
ai-dev flow complete ...
ai-dev flow block ...
ai-dev flow resume ...

ai-dev summarize ...
ai-dev summarize-verify ...
ai-dev review-verify ...
ai-dev config
ai-dev apply
```

Use `ai-dev config` as the supported configuration editing path.
Use `ai-dev apply` to reconcile managed launchers and PATH configuration.

## Adding managed command aliases

Edit your user config file and add aliases under `installation.aliases`.

- Open or create the file with `ai-dev config`.
- Linux default path: `~/.config/ai-dev/config.yaml`.
- Windows default path: `%APPDATA%/ai-dev/config.yaml`.

Copyable example:

```yaml
installation:
  aliases:
    enabled: true
    expand_subcommands: true
    commands:
      flow: "ai-dev flow"
      my-alias: "ai-dev some-command"
```

  Example result:

  - `my-alias` -> `ai-dev some-command`

  After saving the config, run:

  ```text
  ai-dev apply
  ```

  This creates and reconciles managed launcher files.

Field meanings:

- `enabled`: whether configured aliases are installed/reconciled.
- `expand_subcommands`: global descendant expansion policy for configured roots.
- `commands`: alias-name to command mapping.
- String command syntax is the normal form.
- Argv arrays are the advanced exact-token form (tokens are preserved verbatim).

Existing-config migration example:

```yaml
# Remove or ignore this legacy top-level block:
aliases: {}

# Add managed launcher configuration here:
installation:
  aliases:
    enabled: true
    expand_subcommands: true
    commands:
      flow: "ai-dev flow"
```

Top-level `aliases` is an obsolete configuration field retained in some older user config files. It is not used for managed launchers, and `ai-dev apply` ignores it.

Unsupported targets still install their root launcher, but do not receive generated descendants because no authoritative command model is available.

`ai-dev config` preserves existing config files byte-for-byte.
If an existing file still contains stale comments or old layout, the command will not rewrite or migrate it automatically.

On Linux, `ai-dev apply` manages executable convenience launchers in
`~/.local/bin` rather than shell aliases in `.bashrc`, enabling normal
`flow-<tab>` completion for launcher names such as `flow-commit`.

During Issue #19 migration, top-level lifecycle routes may remain temporarily
supported as compatibility entry points. Canonical lifecycle usage is
under `ai-dev flow ...`.

Managed alias config for `ai-dev apply` lives under `installation.aliases`:

```yaml
installation:
  aliases:
    enabled: true
    expand_subcommands: true
    commands:
      flow: "ai-dev flow"
```

Global expansion policy:

- `enabled: false` keeps alias definitions in config but skips managed alias install/reconcile.
- `enabled: true` reconciles managed aliases normally.
- `expand_subcommands: false` installs only configured root aliases.
- `expand_subcommands: true` enables root + direct-subcommand descendant generation using AI Dev's internal command registry (implemented in checkpoint 2).

Use explicit aliases in `commands` for selective behavior.

## Design Principles

- Source is truth.
- Documentation is an AI navigation layer.
- Documentation should be reviewable.

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
