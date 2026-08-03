# Managed Aliases Slice 2 (Issue #17 Checkpoint 2)

This slice adds managed command alias reconciliation through:

- `ai-dev config apply`
- user-config alias validation (`aliases` mapping in user config)
- deterministic alias-file rendering (POSIX and PowerShell)
- managed profile-block ownership with begin/end markers
- installation manifest ownership validation
- transactional apply/remove-all behavior with rollback reporting

## Generated Alias Functions

Managed aliases forward all user arguments directly to `ai-dev` commands.

Historical note: this checkpoint snapshot predates canonical lifecycle namespace hardening; lifecycle aliases should now target `ai-dev flow ...`.

POSIX generated functions:

```sh
review() {
  command ai-dev flow review "$@"
}
```

PowerShell generated functions:

```powershell
function review {
  & ai-dev flow review @args
}
```

Notes:

- POSIX generated files do not set shell options such as `set -e`.
- PowerShell generated files do not use `Invoke-Expression`.

## Shell/Profile Resolution

`config apply` resolves a single profile target deterministically:

- `$SHELL` basename `bash` -> `~/.bashrc`
- `$SHELL` basename `zsh` -> `~/.zshrc`
- unsupported shell -> actionable error asking for `bash` or `zsh`

Windows profile target remains:

- `~/Documents/WindowsPowerShell/Microsoft.PowerShell_profile.ps1`

Manifest path resolution is independent from profile selection:

- POSIX: `~/.config/ai-dev/managed-aliases-manifest.json`
- Windows: `~/.ai-dev/managed-aliases-manifest.json`

Path resolution is injectable in tests through explicit installer paths.

## User Config

Aliases are read from user config under:

```yaml
aliases:
  gs: status
  gstart: start
```

Validation rules:

- alias names must match `^[A-Za-z_][A-Za-z0-9_]*$`
- hyphens are rejected (no automatic normalization)
- alias names must not contain shell metacharacters or whitespace
- alias targets must be a single supported `ai-dev` top-level command
- on Windows normalization, aliases are checked case-insensitively for collisions

## Profile Path Escaping

Profile source lines keep paths literal and apply shell-native single-quote escaping.

POSIX escaping:

- input: `/home/o'brien/x`
- rendered: `'/home/o'"'"'brien/x'`

PowerShell escaping:

- input: `C:\Users\O'Brien\x`
- rendered: `'C:\Users\O''Brien\x'`

No shell evaluation or `Invoke-Expression` is used.

## Manifest Ownership Checks

Manifest load validates:

- path fields are absolute and normalized
- digest fields are lowercase 64-character SHA-256 strings

Before overwriting/removing an existing generated alias file, apply verifies one of:

- alias file matches prior manifest path+digest
- alias file clearly contains AI Dev generated ownership header

If neither is true, apply refuses to modify a divergent user-owned alias file.

For migration/remove-all cleanup of old artifacts, alias ownership is stricter:

- old alias file path must match prior manifest alias path
- old alias file digest must match prior manifest digest
- divergent/missing old alias file causes safe failure with manual recovery guidance

## Reconciliation and Transactionality

`config apply` performs:

1. Ensure user config exists.
2. Load and validate desired alias state.
3. Resolve manifest path first and load previous manifest.
4. For non-empty aliases, resolve desired current installer paths.
5. Build either same-path apply plan or manifest-driven migration plan.
6. Execute the plan transactionally.
7. Persist/update/remove manifest last.

When prior manifest paths differ from desired current paths, apply performs migration:

- validate old alias ownership/digest against prior manifest
- remove the managed block from old profile path
- remove old generated alias file
- install/update new alias file and new profile block
- write new manifest last

Strict old-alias verification is applied for every migration, including profile-only migrations where alias-file path is unchanged.

No successful migration leaves both old and new profile integrations active.

Both non-empty apply/migrate and remove-all are transactional:

- snapshot all touched old/new alias/profile files plus manifest state before mutation
- execute requested mutation sequence
- on failure, restore snapshot best-effort
- if rollback has failures, report original failure plus rollback failures including exact path/operation details

The implementation does not silently swallow rollback errors.

## Remove-All Across Shell Changes

When desired aliases are empty and a prior manifest exists, cleanup uses manifest-recorded paths, not current shell detection:

- remove managed block from manifest profile path
- remove manifest alias-file path (digest-verified)
- remove manifest last

This works even if current `$SHELL` is unsupported or empty.

## Managed Profile Block Semantics

The profile block is owned only between markers:

- `# >>> ai-dev managed aliases >>>`
- `# <<< ai-dev managed aliases <<<`

Updates remove/replace only the managed block plus the AI Dev-owned separator newline directly attached to the block. Bytes outside the owned block are preserved.

This includes preservation across:

- beginning/middle/end placement
- no-final-newline files
- adjacent blank lines
- idempotent re-apply behavior

## CLI Result Semantics

`config apply` reports:

- `Result: applied`
- `Result: migrated`
- `Result: no-op`
- `Result: removed-all`

Manifest reporting distinguishes write vs removal:

- `manifest` listed in updates only when written
- `manifest-removed` listed in updates only when remove-all deleted it

## Testing

Focused test modules for this slice:

- `tests/test_alias_config.py`
- `tests/test_profile_blocks.py`
- `tests/test_installation_manifest.py`
- `tests/test_alias_installation.py`
- `tests/test_config_apply_cli.py`
