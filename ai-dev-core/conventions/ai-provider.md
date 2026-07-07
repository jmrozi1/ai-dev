# AI Provider Configuration

## Purpose

Define how `ai-dev-vscode` executes AI commands and handles prompt delivery.

Projects configure a provider in `.ai-dev.yaml` using the `aiProvider` key. The provider layer is intentionally decoupled from any specific AI product. It must not assume Copilot, Claude, Codex, ChatGPT, or any other vendor.

---

## Supported Modes

### `prompt-only`

Commands build a prompt bundle but do not execute any AI call. The prompt bundle is opened or displayed so the user can send it manually to whatever AI tool is available.

```yaml
aiProvider:
  mode: "prompt-only"
```

**When to use:**

- Restricted environments where no AI CLI is available or permitted.
- Restricted or disconnected systems where provider-backed execution is not configured.
- Teams that want full control over where the prompt is sent.
- Auditing workflows where human review of the prompt is required before submission.

**Behavior:**

- The extension assembles the full prompt bundle for the requested command (generate, review, ask, etc.).
- The prompt bundle is shown to the user rather than dispatched to an AI backend.
- No files are written automatically.
- The user copies the prompt, submits it to their chosen AI tool, and applies the result manually.

`prompt-only` is the safest fallback. It requires no external tooling and introduces no automated file writes.

---

### `command`

The extension pipes the prompt bundle to a local shell command on stdin and captures stdout.

```yaml
aiProvider:
  mode: "command"
  command: "<command-name-or-path>"
  args:
    - "<arg1>"
    - "<arg2>"
```

**When to use:**

- A team-managed or internally approved AI CLI is available.
- Automation pipelines where manual prompt delivery is not acceptable.

**Behavior:**

- The extension launches the configured command with the provided args.
- The full prompt bundle is written to the command's stdin.
- The command's stdout is captured as the AI response.
- The command's stderr is captured separately for diagnostics.
- If the command exits non-zero, the extension treats the invocation as failed.

**Output routing:**

| Command type | stdout routing |
|---|---|
| Generate (docs, etc.) | Written to project files |
| Review / Ask | Opened ephemerally and copied to clipboard |

Git diff is the safety gate for generated file changes. The extension does not bypass version control.

---

## Provider Command Requirements

A conforming provider command must:

1. Read the prompt bundle from **stdin**.
2. Write only the final markdown or result to **stdout**. Do not emit banners, progress lines, version strings, or extra logs to stdout — only the content that should appear in the generated file or review result.
3. Write diagnostics, warnings, and error messages to **stderr**.
4. Exit **non-zero** on failure so the extension can detect and surface errors.
5. Be self-contained. The extension does not inject authentication, tokens, or environment variables on behalf of the command. The command is responsible for its own credentials and configuration.

The command runs with the **workspace root as the current working directory**.

---

## Examples

### Default: prompt-only (recommended starting point)

```yaml
aiProvider:
  mode: "prompt-only"
```

Use this when setting up a project for the first time or when no AI CLI is available. Switch to `command` mode once a reviewed and approved CLI is in place.

---

### Plumbing test: `cat`

```yaml
aiProvider:
  mode: "command"
  command: "cat"
  args: []
```

`cat` echoes stdin directly to stdout unchanged. This is a plumbing test only — not a real AI provider. It proves that:

- The extension assembles the prompt bundle correctly.
- The stdin/stdout pipe is wired.
- File-write or ephemeral-display paths execute without errors.

The output will be the raw prompt, not a generated result. Do not leave `cat` configured in production.

---

### Generic internal CLI

```yaml
aiProvider:
  mode: "command"
  command: "internal-ai-cli"
  args:
    - "generate"
    - "--stdin"
```

Replace `internal-ai-cli` with the actual command name or path approved for your team. The command must satisfy the [provider command requirements](#provider-command-requirements) above.

---

## Security and Operational Caution

Provider commands are local shell commands. The extension runs whatever command is specified in `.ai-dev.yaml`.

- **Review command configuration before trusting it.** Understand what the configured command does before enabling it in your environment.
- **Do not configure arbitrary commands from untrusted repositories.** Cloning a repository and running its AI Dev workflows without reviewing `.ai-dev.yaml` is equivalent to running arbitrary scripts.
- **Prefer `prompt-only` in shared or restricted environments** until the command configuration has been reviewed by the relevant team.
- Commands inherit the user's shell environment and filesystem permissions. Ensure the command is appropriate for that trust level.

---

## Relation to Other Rules

- [git-integration.md](git-integration.md) — Git diff as the safety gate for generated file changes.
- [documentation-layout.md](documentation-layout.md) — Where generated files are written.
