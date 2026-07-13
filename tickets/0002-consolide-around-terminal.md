TICKET: Consolidate AI Dev Around the Terminal Assistant

SUMMARY

Make the terminal assistant the primary interface for AI Dev.

Remove the existing workflow-oriented sidebar and obsolete workflow commands. Replace them with a smaller, intent-based slash-command surface inside the AI Dev terminal.

The public interaction model should become:

- Plain text: ask the assistant using automatic routing
- /ask: explicitly ask with route controls
- /summarize: generate or update project summaries
- /review: review a target using future-extensible review modes
- /settings: configure AI Dev
- /exit: close the session

The goal is to eliminate duplicate entry points, old workflow scaffolding, and command-palette clutter before those become permanent architecture.

USER STORY

As the sole user of AI Dev, I want one coherent terminal-first interface so that I do not need to remember whether a capability lives in the Activity Bar, a workflow tree, the Command Palette, a context menu, or a slash command.

DESIGN PRINCIPLES

1. The terminal assistant is the product surface.
2. Plain chat uses automatic routing by default.
3. Slash commands express user intent.
4. Flags constrain routing or execution.
5. AI is the default unless a deterministic flag such as --help, --config, or --smoketest is used.
6. Remove obsolete commands instead of preserving compatibility aliases.
7. Do not keep skeleton commands that are no longer part of the intended product.
8. Routing documentation helps locate evidence; source/configuration remains authoritative when available.
9. Routing failures should surface as warnings rather than silently falling back.

TARGET COMMAND SURFACE

/ask [question] [route options]
/summarize <path-or-glob> [options]
/review [target] [options]
/settings [options]
/exit

PLAIN CHAT

Entering ordinary text without a slash command is equivalent to:

/ask --auto <question>

The assistant should use its best judgment to determine whether the question should be answered from:

- General chat knowledge
- Summary/routing documentation
- Knowledge-base documentation
- Workspace source code
- External context
- A mixed evidence route

The assistant should not narrate every routing step by default.

Warnings should appear when expected project evidence is unavailable or incomplete.

Example:

◆ Auto: The deployment job is deploy-service-x.

WARNING Unable to verify the referenced Jenkinsfile because it was not found in indexed external context.

COMMAND GRAMMAR

Use conventional CLI-style syntax:

/<command> [positional arguments] [options]

Long flags use two dashes:

--help
--config
--smoketest
--auto
--summary
--knowledgebase
--chat

Short aliases may use one dash:

-h
-s
-k

Do not use single-dash long options such as -help.

COMMON FLAG BEHAVIOR

--help or -h

- Deterministic
- Does not invoke AI
- Prints purpose, syntax, arguments, flags, and examples for the command

--config

- Deterministic
- Does not invoke AI
- Opens or displays configuration relevant to the command
- May map to /settings where appropriate

--smoketest

- Deterministic
- Does not invoke AI
- Resolves inputs
- Validates configuration
- Previews intended work
- Does not perform model calls or write generated output
- Prints a bounded preview and aggregate counts

/ASK

Syntax:

/ask [question] [options]

Supported route options:

--auto
--summary
-s
--knowledgebase
-k
--chat

Behavior:

- /ask with no route flag behaves as --auto
- Plain text behaves as /ask --auto
- --auto chooses the best route
- --summary restricts routing to summary/routing documentation
- --knowledgebase restricts routing to the knowledge base
- --chat bypasses project routing and answers as general chat
- --chat remains available even though plain chat defaults to auto, because it is an explicit routing override
- Conflicting route flags produce a permanent warning or error
- Missing required question text produces compact usage guidance

Future-compatible route reporting should include:

- Chosen route
- Confidence
- Evidence used
- Missing expected evidence
- Warnings
- Final answer

The terminal should show only meaningful warnings by default. Detailed routing diagnostics may be exposed later through a report/debug command.

/SUMMARIZE

Syntax:

/summarize <path-or-glob> [options]

Behavior:

- A file path replaces the old single-file "Generate Summary Doc" workflow
- A glob replaces the old batch-summary workflow
- The command determines target summary files using current project conventions
- Summary generation may use AI unless --smoketest or another deterministic option is used
- After successful summary updates, refresh the architecture summary automatically
- Architecture summary generation is no longer a separate primary command
- If architecture refresh fails, keep successful summary writes and print a permanent warning
- Do not roll back completed summary writes because architecture refresh failed

Example:

/summarize ./lib/*.jenkins --smoketest

Expected smoke-test output includes:

- Total matched source files
- Planned summary targets
- Estimated model-call count
- First 10 matched files
- Count of omitted preview entries when more than 10 exist
- A report reference or future report identifier when applicable

/REVIEW

Syntax:

/review [target] [options]

Behavior:

- Replaces the old "Review Summary Docs" command
- The old "Review Summary Doc for Selected File" command is removed
- /review is intentionally general so later modes can include:
  - Documentation review
  - Code review
  - Test review
  - Ticket review
- This ticket does not need to implement all future review modes
- The parser and help text should leave room for future options such as:
  - --docs
  - --code
  - --tests
  - --ticket
- Current behavior may initially map to the existing documentation-review workflow

/SETTINGS

Syntax:

/settings [options]

Behavior:

- Opens AI Dev settings
- /settings and /settings --config may perform the same action
- /settings --help prints deterministic help
- Settings remain optional and generic defaults continue to work without .ai-dev.yaml

/EXIT

Syntax:

/exit

Behavior:

- Cleanly closes the AI Dev terminal session
- /exit is the intentional exception to the general AI-command pattern
- It must remain local and deterministic

ACTIVITY BAR

1. Remove the current Workflows section from the AI Dev sidebar.
2. Remove workflow-tree items for:
   - Generate Summary Doc
   - Batch Summary Doc Generation
   - Generate Architecture Summary
   - Review Summary Docs
   - Review Summary Doc for Selected File
   - Answer From Summary Docs
3. Do not add replacement workflow rows for slash commands.
4. Keep the AI Dev Activity Bar icon only if it provides a clean launcher experience.
5. Preferred behavior: selecting the AI Dev Activity Bar icon launches or reveals the existing AI Dev assistant terminal.
6. If VS Code cannot directly bind an Activity Bar icon to a command:
   - Use the smallest possible launcher view
   - Do not recreate a workflow menu
   - The view may contain one prominent "Open AI Dev Assistant" action
7. Do not keep a Settings row in the sidebar; settings are available through /settings.
8. The Activity Bar icon must not create duplicate AI Dev terminals.

COMMAND PALETTE AND CONTRIBUTIONS

Keep only the minimum public Command Palette surface:

- AI Dev: Launch Assistant

Optionally keep:

- AI Dev: Settings

Remove obsolete public commands and activation events for:

- Generate Summary Doc for Selected File
- Batch Summary Doc Generation
- Generate Architecture Summary
- Review Summary Docs
- Review Summary Doc for Selected File
- Answer From Summary Docs
- Copilot Test, unless intentionally retained as a hidden/internal diagnostic command

Remove obsolete menu contributions from:

- Explorer context menu
- Editor context menu
- Source Control title menu
- AI Dev workflow submenu

Do not preserve compatibility aliases for removed commands.

Existing internal workflow functions may be reused behind slash commands, but dead public command scaffolding should be deleted when no longer needed.

TERMINAL COMMAND DISCOVERY

1. Replace startup guidance "/help for commands" with:

   Type / for commands · Esc returns to chat

2. Typing "/" immediately displays the available commands below the prompt.
3. Command lookup remains ephemeral.
4. Partial substring matches are allowed.
5. Matching text remains highlighted using the existing #EEEEFF lookup color.
6. Command lookup should include:

   /ask
   /summarize
   /review
   /settings
   /exit

7. As command options are implemented, lookup/help should expose relevant flags.
8. Tab completion and double-Tab listing continue to work.
9. /help may remain as a global alias if desired, but typing "/" is the primary discovery path.
10. Escape leaves command mode and returns to normal chat.

ROUTING WARNINGS

Automatic routing should warn only when project evidence was reasonably expected.

Examples that should warn:

- Project-specific question with no routing documentation
- Summary points to source that cannot be found
- Jenkins job summary cannot be verified against configuration
- Documentation and source disagree
- Knowledge-base route was explicitly forced but unavailable

Examples that should not warn:

- General conceptual question unrelated to the project
- Explicit --chat request
- Project question successfully verified through available evidence

Warnings are permanent yellow terminal output and include the word WARNING.

OUT OF SCOPE

- Full Jenkins dependency mapping
- Ticket management system
- Code-generation implementation
- Test-generation implementation
- All future /review submodes
- Persistent terminal sessions across VS Code restarts
- /showreport
- /debug
- Rich links
- Multiple concurrent assistant sessions
- Backward compatibility for removed commands

ACCEPTANCE CRITERIA

1. Plain terminal input is routed through /ask --auto behavior.
2. /ask exists and supports --auto, --summary/-s, --knowledgebase/-k, and --chat.
3. /ask with no route flag uses --auto.
4. Conflicting /ask route flags produce a permanent warning or error.
5. /summarize accepts a file path.
6. /summarize accepts a glob.
7. /summarize --smoketest performs no model calls and no writes.
8. /summarize --smoketest prints counts and at most the first 10 matched files.
9. Successful summarization triggers architecture-summary refresh.
10. Architecture refresh failure does not undo completed summary writes.
11. /review replaces the existing documentation-review entry point.
12. The old selected-file review command is removed.
13. /settings opens settings.
14. /settings --help prints deterministic help.
15. /settings --config opens settings.
16. /exit cleanly closes the terminal.
17. Typing "/" displays the consolidated command list below the prompt.
18. Escape returns to chat mode.
19. Tab completion continues to work.
20. The old Workflows sidebar section is removed.
21. Obsolete workflow rows are removed.
22. Obsolete public Command Palette commands are removed.
23. Obsolete context-menu and submenu contributions are removed.
24. The AI Dev Activity Bar icon launches or reveals the assistant through the smallest practical launcher surface.
25. Launching through the Activity Bar does not create duplicate terminals.
26. Existing generic configuration defaults continue to work without .ai-dev.yaml.
27. Automatic routing emits warnings when expected evidence cannot be found.
28. General chat questions do not emit irrelevant routing warnings.
29. Explicit --chat bypasses project routing.
30. Compile passes.
31. Lint passes.
32. Automated tests pass.
33. Manual smoke test passes in the supported VS Code environment.

TEST EXPECTATIONS

Add focused tests for:

- Command parsing into command, positional arguments, and options
- Long and short flag aliases
- Conflicting route flags
- Plain chat mapping to ask-auto
- /ask default route
- /ask --summary and -s
- /ask --knowledgebase and -k
- /ask --chat
- /summarize file target
- /summarize glob target
- /summarize --smoketest
- Smoke-test preview limited to 10 displayed files
- Architecture-refresh sequencing
- Architecture-refresh failure warning behavior
- /settings, --help, and --config
- /review parsing
- /exit
- Consolidated command lookup
- Removed workflow-tree nodes
- Removed package.json command contributions
- Terminal reuse from Activity Bar launch
- No-.ai-dev.yaml generic behavior remains intact

MIGRATION NOTES

This is an intentional cleanup, not a compatibility release.

Delete obsolete public command surfaces rather than hiding them.

Reuse internal implementation where useful, but do not preserve old command names, old workflow-tree structure, or duplicate interaction paths merely because they already exist.