# Obsolete Plugin Cleanup Checkpoint 1

## Scope

Checkpoint 1 removes the production VS Code language-model execution path and the old session-owned chat surface. Summarize and review now resolve to external generated-task handoffs instead of in-extension model calls.

The user-facing surfaces that remain in place are:

- `ai-dev summarize`
- `ai-dev summarize-verify`
- `ai-dev flow review`
- `ai-dev review-verify`
- `ai-dev config`
- `ai-dev config apply`
- `ai-dev --help`
- `ai-dev <command> --help`
- generated tasks
- report handoff
- verification
- bootstrap
- aliases
- editable config

## Removed Production Surface

- `ai-dev-vscode/src/assistantChatBackend.ts`: deleted the direct `vscode.lm` / `LanguageModelChat` backend entirely.
- `ai-dev-vscode/src/assistantInput.ts`: removed `/ask` parsing and session-oriented command resolution.
- `ai-dev-vscode/src/assistantCommands.ts`: removed `/ask` from command discovery and help output.
- `ai-dev-vscode/src/test/extension.test.ts`: restored the checkpoint-0 suite, then removed only direct-execution `/ask` and backend lifecycle tests and added generated-task handoff coverage.

## Remaining Runtime Surface

- `ai-dev-vscode/src/assistantTerminal.ts`: keeps the terminal UI, slash-command plumbing, report handoff, and summarize/review external handoff messaging.
- `ai-dev-vscode/src/config.ts`: keeps config loading for summarize/review workflows.
- `ai-dev-vscode/src/settingsWorkflow.ts`: keeps the editable config workflow.
- `ai-dev-vscode/src/projectReview.ts`: keeps review preparation and prompt assembly.
- `ai-dev-vscode/src/summarizationWorkflow.ts`: keeps summarization preparation and prompt assembly.
- `ai-dev-vscode/src/modelContextLimits.ts`: keeps context-budget constants used by summarize/review preparation.
- `ai-dev-vscode/src/extension.ts`: keeps command registration and terminal/report wiring.

## Quantitative Inventory

Counts use checkpoint 0 (`HEAD`) as the before state. Production and test LOC count TypeScript files under `ai-dev-vscode/src`, with `src/test` reported separately.

| Surface | Before | After |
| --- | ---: | ---: |
| Production source files | 35 | 35 |
| Production source LOC | 17,788 | 16,948 |
| Runtime dependencies | 0 | 0 |
| Test files | 1 | 1 |
| Test LOC | 7,124 | 6,640 |
| Commands removed | 0 | 2 (`/ask`, `/exit`) |
| Config keys removed | 0 | 0 |

The production file count remains 35 because deleting `assistantChatBackend.ts` is balanced by adding the thin `generatedTaskHandoff.ts` adapter. Unrelated retained tests for configuration, dependency maps, summarization rules, source verification, routing, reports, and other retained production modules were preserved.

## Notes

- This checkpoint is no longer about a compatibility shim or stateless adapter. The production backend is deleted.
- Summarize and review remain as terminal-driven handoff flows that invoke the canonical `ai-dev summarize` and `ai-dev flow review` task-preparation commands. The extension does not define a separate task format.

Historical note: this checkpoint originally retained top-level lifecycle compatibility routes during Issue #19 migration; canonical lifecycle usage is now `ai-dev flow ...`.
- Checkpoint 2 has not started; terminal UI, VSIX, vendor, build, and `showreport` architecture remain in scope for later work.