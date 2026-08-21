---
name: report
description: Print the latest completed repository-scoped Copilot work report through the canonical Flow renderer.
user-invocable: true
disable-model-invocation: true
---

# Copilot Report

Use the packaged adapter for this environment:

- [POSIX adapter](./scripts/flow-report)
- [PowerShell adapter](./scripts/flow-report.ps1)

Each adapter resolves its own installed or source location, finds the sibling
Flow skill package, and delegates to that package's existing report helper.
Return the canonical helper output unchanged without synthesizing, summarizing,
modifying, or reimplementing the report, repository, Flow state, telemetry,
settings, approvals, or logs.
