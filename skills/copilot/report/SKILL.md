---
name: report
description: Print the latest completed repository-scoped Copilot work report through the canonical Flow renderer.
user-invocable: true
disable-model-invocation: true
---

# Copilot Report

Run the installed Copilot Flow helper `scripts/flow-report` from the current
repository and return its output unchanged. The helper is the sole execution
adapter; it owns source discovery and parser-health handling, while the shared
Python renderer owns report semantics. Do not synthesize, summarize, or modify
the report, repository, Flow state, telemetry, settings, approvals, or logs.
