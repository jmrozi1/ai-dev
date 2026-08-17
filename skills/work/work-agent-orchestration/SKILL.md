---
name: work-agent-orchestration
description: Help the user direct, debug, and improve constrained work AIs. Use whenever the user is relaying instructions to a work AI, asking what to tell a work AI next, diagnosing work-agent behavior, or refining the CLAUDE.md instructions, permissions, or skills used by those agents.
---

# Work Agent Orchestration

Use this skill when helping the user operate constrained work AIs through manual back-and-forth or when refining the controls that make those agents reliable.

## Load Current Work-Agent State

Before advising on work-agent orchestration, read:

- `references/CLAUDE.md`
- `references/settings.json`

Treat these as the tracked reference copies of the current work-agent instructions and permissions. When the user reports a persistent change to the real work environment, update the corresponding reference so future orchestration uses the new baseline.

Do not assume the references are current when the user explicitly says the work copy differs. Reconcile the reference with the reported work copy first.

## Minimize Relay Cost

The user may need to manually type or relay instructions into the work environment.

Prefer the smallest instruction that should reliably produce the desired result. Do not make the user relay background explanation, reasoning, or defensive detail that the work agent does not need to execute the current step.

When a longer instruction is necessary because of demonstrated work-agent behavior, keep it explicit and operational.

## Work Empirically

Treat work-agent use as both task execution and process refinement.

For each interaction:

1. Identify the immediate outcome the work agent should produce.
2. Give the smallest practical instruction to relay.
3. Observe the work agent's actual behavior from the user's report.
4. If the behavior is unacceptable, identify the failure class.
5. Decide which control layer should address it.
6. Make the smallest change that prevents the demonstrated failure.
7. Retest through the work agent before adding further restrictions.

Do not speculate about every possible failure in advance.

## Independently Validate Executor Work

When an orchestrator reviews work performed by a separate executor, do not treat the executor's summary, claimed success, or description of changes as sufficient validation.

Before accepting the work, declaring it complete, or issuing the next dependent task, independently inspect the actual output and the evidence required by the assignment.

For repository changes, inspect the relevant diff and changed files. For claimed validation such as tests or command results, inspect the actual reported output or independently rerun the appropriate read-only validation when practical.

If the orchestrator does not yet have enough evidence to make the judgment, gather the missing evidence or instruct the executor to return it before accepting the work.

## Choose the Correct Control Layer

When a work-agent failure reveals a process problem, classify the fix before changing anything.

Use temporary task instructions when the requirement is specific to the current task.

Use `references/CLAUDE.md` when the rule should apply to ordinary work-agent behavior across tasks in that environment.

Use `references/settings.json` when the issue is permission enforcement or repeated permission friction that can be addressed through Claude Code configuration.

Use a work-agent-specific skill when the behavior belongs to a recurring type of work and the constrained work models need specialized guidance that should not affect stronger/shared agents.

Use a shared/global skill only when the behavior is broadly desirable independent of the constrained work-agent environment.

Do not promote a work-agent workaround into shared guidance merely because it improved the constrained model.

## Preserve Useful Capability

Prefer outcome-oriented instructions until the work agent demonstrates that they are insufficient. Add mechanical constraints only to address observed failures, non-negotiable requirements, or safety/correctness invariants.

When a work-agent-specific skill needs refinement, use `skills/work/write-low-reasoning-skills/SKILL.md`.

## Track Persistent Changes

When the user and assistant agree that a new instruction or permission should become part of the work-agent baseline:

- update the appropriate tracked reference;
- preserve unrelated existing settings and instructions;
- distinguish experimental changes from accepted persistent behavior;
- avoid accumulating obsolete rules after a better control supersedes them.

## Output Style During Orchestration

When telling the user what to relay to a work AI:

- lead with the exact instruction to send;
- keep it short enough to relay comfortably;
- add explanation only when it helps the user evaluate the instruction or diagnose the response;
- if the work agent reports an unexpected result, reassess rather than continuing from an assumed state.
