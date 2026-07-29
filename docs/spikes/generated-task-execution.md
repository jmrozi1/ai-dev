# Generated-Task Execution Spike Report

## Summary
This spike evaluated whether externally generated Markdown task files can drive reliable AI execution for AI Dev workflows. Across three scenarios, the task-file handoff model produced the expected summary artifacts, preserved source files, and respected explicit constraints and format requirements. The evidence supports migration to a script-and-task-file architecture, with deterministic validation owned by AI Dev scripts and semantic work delegated to the external AI.

## Hypothesis
Generated Markdown task files plus a compact invocation are sufficient to orchestrate reliable AI work, so AI Dev does not need to own chat or model orchestration internals.

## Scenarios Executed
1. Test 001: small task-file handoff via a compact invocation referencing only the task-file path.
2. Test 002: mixed instruction model using both general requirements and pattern-specific requirements.
3. Test 003: medium batch across nine listed source paths, including one missing source path and one large source file.

## Results
1. Test 001 succeeded on core goals.
- All three requested sources were processed.
- All three expected summary outputs were created.
- Required headers were present.
- Completion report was created.
- No tracked source modifications were reported.

2. Test 002 succeeded on rule precedence goals.
- General instructions were applied across both files.
- Pattern-specific sections appeared as required: Configuration Contract and Persistence Contract.
- Unverified behavior was explicitly reported instead of being invented.
- No tracked source modifications were reported.

3. Test 003 succeeded on throughput and large-file handling.
- Nine per-file summaries plus completion report were produced.
- Required headers and required file-specific sections were present.
- Large source summary generation for ai_dev_flow/cli.py succeeded.
- No tracked source modifications were reported.
- ai_dev_flow/__main__.py was missing; the AI did not invent behavior and produced a source-missing summary.
- Reliability caveat: the AI-written completion report still counted that source as completed, reported nine written summaries, and listed no skipped files.

## Reliability Findings
1. Generated Markdown task files are a viable replacement for AI Dev owning its own chat/model interface.
2. A compact invocation containing only the task-file path is sufficient for execution handoff.
3. General and pattern-specific instructions work as intended.
4. Medium multi-file workloads and large files work.
5. External AI completion reporting is advisory and cannot be treated as authoritative system status.
6. Missing-source handling must be determined by script policy, not inferred from AI prose.

## Required Deterministic Controls
AI Dev scripts should own deterministic controls before and after AI execution:
1. Preflight source validation:
- Resolve exact source paths.
- Reject or classify nonexistent sources before task execution.

2. Manifest and expansion controls:
- Expand globs deterministically.
- Build and persist an expected-output manifest.

3. Source-preservation controls:
- Enforce no-source-modification checks.
- Fail or flag when protected paths are changed.

4. Status accounting and freshness controls:
- Compute completion status from manifest checks, not AI narrative.
- Verify timestamps and content freshness where required.

5. Post-execution artifact validation:
- Validate required headers, required sections, and output count.
- Reconcile produced outputs against expected outputs.

## Recommended Task Protocol
Recommended protocol:
1. Use immutable Markdown task files as the execution contract.
2. Include exact source paths and exact output paths.
3. Require machine-readable output headers.
4. Include explicit constraints and forbidden actions.
5. Include an expected-output manifest.
6. Use compact invocation text that references only the task-file path.
7. Run deterministic verification after execution.

Recommended status header per output file:
- Summary-Status: complete

Additional status values:
- source-missing
- failed
- partial

Final status authority:
- AI Dev computes final status independently from deterministic checks and manifest reconciliation.

## Batching Recommendation
Bounded batching should remain part of migration design. The medium batch succeeded, but bounded batch size still reduces risk, improves retry behavior, and simplifies deterministic verification and error isolation.

## Go/No-Go Decision
GO.

Proceed with script-and-task-file migration. Based on this spike evidence, AI Dev no longer needs to own:
- chat rendering;
- direct model calls;
- conversation history;
- context meters;
- terminal interaction.

AI Dev should instead own deterministic orchestration, validation, and status computation around external AI execution.

## Follow-Up Work
1. Add preflight classification for nonexistent or unreadable source paths with explicit policy outcomes.
2. Implement manifest-driven post-execution validator for required outputs, headers, sections, and status accounting.
3. Add source-preservation guardrails that fail on unexpected source changes.
4. Add freshness checks and rerun semantics for stale or partial outputs.
5. Pilot this protocol across additional repositories and at least one additional AI provider or IDE integration before broad rollout.
