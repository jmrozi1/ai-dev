# Managed Launcher Aliases (Checkpoint 2 Expansion)

This document defines the managed alias configuration model consumed by `ai-dev apply`.

## Adding managed command aliases

Managed launcher config is edited in the AI Dev user config file:

- Open the file with `ai-dev config`.
- Linux default path: `~/.config/ai-dev/config.yaml`.
- Windows default path: `%APPDATA%/ai-dev/config.yaml`.

Add managed aliases under `installation.aliases`.

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

Plain meaning of each level:

- `enabled`: whether configured aliases are installed/reconciled.
- `expand_subcommands`: global descendant expansion policy.
- `commands`: alias-name to command mapping.
- String syntax is the normal form.
- Argv arrays are the exact-token advanced form (tokens are stored exactly as provided).

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

`ai-dev config` preserves an existing config file byte-for-byte.
It creates a default file only when missing, so stale comments/layout in existing files are not automatically replaced or migrated.

Checkpoint 2 scope in this issue:

- schema + validation from checkpoint 1
- implemented direct-subcommand expansion for eligible roots
- deterministic suppression/reporting and owned-resource reconciliation for generated descendants

Checkpoint 2 non-goal:

- do not recursively expand nested command trees

## What Is A Managed Command Alias

A managed command alias is a generated launcher file owned and reconciled by AI Dev.
Launchers are regular executables (`~/.local/bin/<name>` on POSIX, `<name>.cmd` on Windows), so normal shell completion discovers names such as `flow-status`.

Each alias maps to a command and is normalized to argv internally. At runtime, launcher invocations forward user arguments unchanged.

Example:

```yaml
installation:
  aliases:
    enabled: true
    expand_subcommands: true
    commands:
      flow: "ai-dev flow"
```

Advanced exact-token form is also accepted:

```yaml
installation:
  aliases:
    commands:
      example:
        - ai-dev
        - command
        - "one argument"
```

Both forms normalize to argv internally.
For argv-array form, tokens are preserved verbatim (including intentional leading/trailing spaces).

## Final YAML Schema

Managed alias configuration lives under `installation.aliases`:

```yaml
installation:
  aliases:
    enabled: <boolean>
    expand_subcommands: <boolean>
    commands:
      <alias-name>: <string-or-argv-array>
  shellPath:
    enabled: <boolean>
```

Rules:

- `installation.aliases` must be a mapping
- only `enabled`, `expand_subcommands`, and `commands` are allowed under `installation.aliases`
- `enabled` must be boolean when present (default: `true`)
- `expand_subcommands` must be boolean when present (default: `true`)
- `commands` must be a mapping when present
- alias names must match `^[A-Za-z_][A-Za-z0-9_-]*$` and are checked against reserved names (`ai-dev`, `aidev`, `ai_dev`)
- each command value may be either:
  - a non-empty string command
  - a non-empty argv array of non-empty string tokens
- argv-array token validation checks `token.strip()` for emptiness, but stores the original token text unchanged

## Global Subcommand Expansion Policy

`expand_subcommands` is one global policy for all configured aliases.

- `false`: install only root aliases explicitly listed in `commands`
- `true`: install root aliases and generated direct-subcommand descendants for each root alias

There is no per-alias expansion toggle.

Users needing selective behavior should disable expansion and define explicit aliases.

`enabled` remains semantically distinct from `commands: {}`:

- `enabled: false`: keep definitions in config but skip managed install/reconcile.
- `enabled: true` with `commands: {}`: no managed alias definitions are present.

## Descendant Naming And Scope

Generated descendants use `<root>-<subcommand>` naming.

Example intent:

- `flow` -> `ai-dev flow`
- `flow-help` -> `ai-dev flow --help`
- `flow-start` -> `ai-dev flow start`
- `flow-status` -> `ai-dev flow status`

Scope is direct subcommands only. Nested command trees are not recursively expanded.

Authoritative subcommand discovery should come from AI Dev's internal command model when possible, not by scraping human-formatted help output.

Implemented authoritative source:

- `ai-dev flow` descendants are derived from registry metadata in `COMMAND_SPECS` / `FLOW_LIFECYCLE_COMMANDS`
- `flow-help` maps to `ai-dev flow --help`
- lifecycle descendants map to `ai-dev flow <subcommand>`

## Collision And Ownership Semantics

Managed launchers are ownership-tracked and reconciled by `ai-dev apply`.

- AI Dev updates/removes only launchers it owns (manifest + marker validated)
- stale AI Dev-managed launchers no longer desired are removed
- unmanaged/user-owned collisions fail closed with a clear error
- non-managed files are never claimed silently

When expansion is active, explicit aliases in `commands` are the source of truth for roots.
Implemented checkpoint 2 collision policy:

- explicit aliases in `commands` win over generated descendants
- the generated descendant is omitted
- `ai-dev apply` reports the suppression clearly

Generated descendants are fully managed resources and follow the same ownership checks as explicit managed launchers.
Stale generated descendants are removed when expansion disables, roots are removed, roots change to non-expandable targets, or command-model descendants change.

## Platform Behavior

POSIX:

- generated launchers are executable files in `~/.local/bin`
- launchers exec configured argv and forward `"$@"`

Windows `.cmd` launchers:

- generated launchers are `.cmd` files in `%LOCALAPPDATA%/ai-dev/bin` (home fallback when unset)
- launchers forward `%*`

Linux PATH block management remains controlled by `installation.shellPath.enabled` and reconciled by `ai-dev apply`.

## Applying Changes

After editing config, run:

```text
ai-dev apply
```

`ai-dev apply` is idempotent and reconciles managed launchers, ownership manifest, and Linux PATH marker state.

## Compatibility Decision

Supported:

- `commands.<name>: "..."` string form (documented normal syntax)
- `commands.<name>: ["...", "..."]` argv array form (advanced exact-token syntax)

Eligibility rule for descendant expansion:

- expansion is attempted only when an authoritative model exists for the configured root command target
- checkpoint 2 guarantees this for `ai-dev flow`
- unrecognized external commands still install root aliases but do not generate descendants

Rejected with clear validation error:

- unknown keys under `installation.aliases`
- malformed command values (empty/unknown/non-string tokens)

This keeps migration practical without silently reinterpreting ambiguous structures.

## Validation Matrix (Checkpoint 3)

- Linux unit coverage: managed launcher planning, expansion eligibility, suppression precedence, stale cleanup, manifest reconciliation, ownership safety.
- Linux integration coverage: generated launcher execution, argument forwarding, exit-status propagation, executable-bit checks, idempotent re-apply behavior.
- Linux shell completion validation: command-name discovery validated via Bash `compgen -c flow-` with managed launcher directory on `PATH` (no custom completion scripts).
- Windows mocked coverage: deterministic `.cmd` rendering, `%*` forwarding, percent escaping, embedded quote escaping, spaces/backslashes in tokens, case-insensitive collisions.
- Native Windows validation status: not performed in this environment.
