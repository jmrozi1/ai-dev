# Obsolete Plugin Cleanup Checkpoint 2

## Scope

Checkpoint 2 removes the dedicated VS Code assistant terminal, chat-input state machine, slash-command UI, and terminal launcher surfaces.

Primary workflows remain the canonical CLI interface:

- `ai-dev summarize ...`
- `ai-dev summarize-verify ...`
- `ai-dev flow review`
- `ai-dev review-verify ...`
- `ai-dev config`
- `ai-dev config apply`
- `ai-dev --help`
- `ai-dev <command> --help`

## Inventory

| Surface | Classification | Decision |
| --- | --- | --- |
| `AiDevAssistantTerminalManager` / `AiDevAssistantPseudoterminal` | Remove now | Deleted `ai-dev-vscode/src/assistantTerminal.ts` and all extension registrations/usages. |
| Assistant input state/history/tab completion | Remove now | Deleted `ai-dev-vscode/src/assistantInput.ts` and removed related tests/imports. |
| Slash command definitions/help (`/help`, `/summarize`, `/review`, `/settings`, `/showreport`) | Remove now | Deleted `ai-dev-vscode/src/assistantCommands.ts`; removed command parser/completion coverage. |
| Terminal launch command and launcher view (`aiDev.launchAssistant`, `aiDev.launcher`) | Remove now | Removed from `package.json`, activation events, extension wiring, and deleted `ai-dev-vscode/src/actionsView.ts`. |
| Terminal ownership/output alignment/spinner/ephemeral formatting | Remove now | Removed with `assistantTerminal.ts`. |
| Terminal-specific settings/config UI path (`/summarize --config`) | Remove now | Removed terminal integration path and deleted terminal-only path completion helper `ai-dev-vscode/src/workspacePathCompletion.ts`. |
| Report opening from terminal (`/showreport`) | Remove now | Removed terminal-only report handoff path with terminal deletion. |
| Summarize/review invocation from terminal | Remove now | Removed terminal handoff flow and deleted now-dead `ai-dev-vscode/src/generatedTaskHandoff.ts`. |
| Editor settings command (`aiDev.settings`) | Retain | Kept as thin editor integration via `settingsWorkflow` and normal command registration. |
| VSIX/build/vendor/install architecture | Defer | No broad packaging cleanup in this checkpoint. |
| Report UX redesign | Defer | Not redesigned in this checkpoint. |

## Quantitative Inventory

Counts use checkpoint-1 `HEAD` as before-state baseline.

| Metric | Before | After |
| --- | ---: | ---: |
| Production source files (`ai-dev-vscode/src`, excluding `src/test`) | 35 | 29 |
| Production source LOC | 16,937 | 14,067 |
| Terminal-specific source files | 5 | 0 |
| Terminal-specific source LOC | 2,604 | 0 |
| Test files (`ai-dev-vscode/src/test`) | 1 | 1 |
| Test LOC | 6,653 | 5,486 |
| VS Code contributed commands | 2 | 1 |
| Slash commands | 5 | 0 |
| Contributed views | 1 | 0 |
| Contributed view containers | 1 | 0 |
| Activation events | 2 | 1 |
| Retained editor commands | 1 | 1 |

## Retained Editor Integration

- `aiDev.settings`: retained because it is independently useful editor integration that edits canonical AI Dev workspace settings without requiring the removed pseudoterminal shell.

## Notes

- Dedicated assistant terminal and pseudoterminal are removed.
- Interactive input/history/tab-completion/slash-command UI is removed.
- Terminal launch command and Activity Bar launcher view are removed.
- No disabled terminal shell remains.
- Canonical CLI workflows remain the primary interface.
- Packaging/vendor cleanup remains deferred to a later checkpoint.
