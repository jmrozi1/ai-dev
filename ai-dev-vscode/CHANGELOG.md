# Change Log

All notable changes to the "ai-dev-vscode" extension will be documented in this file.

## [Unreleased]

## [0.1.0] - 2026-07-16

### Added

- Versioned dependency maps with exact, inferred, ambiguous, and unresolved evidence.
- Jenkins SCM Pipeline resolution from job `config.xml` files to pipeline scripts.
- Recursive delegated-file discovery for Jenkins shell commands and shell `source`/`exec` references.
- Configurable dependency traversal depth, file budgets, character budgets, and inferred-edge handling.
- Dependency-enriched grouped and single-file summaries.
- Source-verified summary answers with direct dependency evidence.
- Architecture-summary table routing and filtering of unsummarized placeholders.

### Changed

- Generated summaries now preserve stable behavioral outcomes, lifecycle, outputs, side effects, and responsibility boundaries.
- Complex orchestration summaries may use compact structured entries instead of being limited to one-line routing metadata.
- Jenkins resolver refresh removes stale generated edges outside the active resolver scope.
