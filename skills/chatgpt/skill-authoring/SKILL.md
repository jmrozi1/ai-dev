---
name: skill-authoring
description: Create, refine, review, split, merge, re-scope, place, or deliberately leave unchanged an AI Dev skill when execution evidence warrants focused skill-quality judgment.
---

# Skill Authoring

Use this ChatGPT capability for judgment about AI Dev skill quality and
ownership. Do not activate it merely because another skill was used. Activate
it when real execution evidence suggests a skill-definition question or when
the task is explicitly creating, refining, reviewing, splitting, merging,
re-scoping, relocating, or deliberately preserving a skill.

## ChatGPT Interaction

This is substantive review/design work. When the invocation begins, announce
`Skill: skill-authoring`, or announce the full responsibility-ordered composed
chain when other skills are materially active. Recommend an advisory reasoning
level, summarize that the review will evaluate activation, guidance sufficiency,
audience placement, composition, helper boundaries, permissions, dogfooding,
and whether no action is correct, then ask `Proceed?`.

Stop before substantial skill-quality analysis until the user confirms. The gate
occurs once per continuous invocation. After proceeding, begin follow-up
responses with the active skill or chain without repeating the reasoning cue,
lens summary, or `Proceed?`. Gate again only for a new invocation, a materially
changed chain, or a scope change that makes a new reasoning decision meaningful.
The recommendation is advisory and does not change or assume the actual ChatGPT
reasoning setting.

## Evidence Before Judgment

Use the smallest evidence surface that supports the decision:

- the skill's activation description and canonical instructions;
- materially used skills and observed activation or omission;
- corrections, interventions, repeated approaches, rediscovery, permission
  friction, scope drift, and validation results;
- tasking, repository, and provider-native execution constraints;
- behavioral dogfooding of positive activation, negative boundaries, and
  intended composition.

Skill use alone is exposure, not evidence of deficiency. A single isolated agent
mistake is not enough to change a skill unless it reveals a durable failure
class or a safety invariant was violated.

## Failure Classification

Distinguish before recommending a change:

- wrong skill activated;
- needed skill failed to activate;
- correct skill activated but guidance was insufficient;
- guidance was sufficient but the executor failed to follow it;
- tasking or provider instructions were deficient;
- isolated, non-actionable agent error.

For wrong or missing activation, identify the smallest responsible owner before
recommending a skill edit. Check, in order as applicable, the canonical skill's
activation metadata or instructions, derivative discovery surfaces such as
`skills/index.md`, caller or provider instructions, and composition/discovery
behavior. Change only the responsible owner: a correct canonical skill should
not be modified to compensate for a discovery-surface, caller-instruction, or
composition defect.

Only the skill-definition cases justify a skill change. Otherwise recommend no
action or a tasking, instruction, permission, or execution correction.

## When To Create, Refine, Or Leave Unchanged

Create or refine a skill when repeated work, durable operational knowledge,
recurring friction, a demonstrated safety/correctness invariant, or strong
behavioral evidence shows that reusable guidance would materially improve future
execution. Keep the guidance proportional to evidence and risk.

`no action` is valid when the evidence is isolated, ambiguous, task-specific,
adequately handled by existing guidance, or better explained by non-skill
failure. Do not preserve obsolete rules or create a skill because an abstraction
is merely imaginable.

## Audience And Placement

Use the Issue #38 audience model:

```text
skills/<name>/                 # genuinely shared operational behavior
skills/chatgpt/<name>/         # ChatGPT judgment or interaction behavior
skills/copilot/<name>/         # Copilot repository execution behavior
skills/work/<name>/            # constrained Work behavior
```

Discovery differences alone do not justify an audience split. Split only when
authority, execution mechanics, recovery, evidence expectations, or reasoning
assumptions materially differ. Keep shared methodology shared. Duplicate names
are valid only for materially different operational implementations, not for
symmetry.

## Activation And Composition

Write a concise positive activation boundary and useful negative boundaries
that prevent adjacent overactivation without excluding work the skill genuinely
owns. The activation description should allow independent discovery without
requiring the user to name the skill.

Evaluate the full current task. Compose multiple skills when each owns a
materially distinct responsibility required by that task. Do not stop after the
first match, but do not load related or potentially useful skills merely for
context. Composition is not a dependency relationship and does not justify a
router, prefix convention, dependency graph, recursive loading, or composition
registry.

Prefer atomic capabilities. For example, requirements, process review,
front-end review, orchestration, and skill authoring should remain separately
usable when their responsibilities differ.

## Helpers And Permissions

Deterministic helpers belong with the audience skill that executes them. Keep
judgment in the skill and mechanics in scripts. Make read-only and mutating
boundaries explicit; for example, evidence gathering should not silently record
state.

Use provider-native permissions for trusted executable skills where available.
Decide whether shell/tools are needed, whether permission is broader than one
skill-local script, and whether a narrower permission is available. Do not add a
custom AI Dev permission system or expand permissions merely to remove prompts.
Reasoning-only skills should not request shell access for convenience.

## Dogfooding And Freshness

Dogfood positive activation, useful negative activation, intended composition,
and the claimed behavior in a fresh session where practical. Refine from real
friction before adding structure or tooling. Keep current operational guidance,
not a transcript or performance history.

When external repository files intentionally control applicability, use the
smallest existing review-trigger mechanism. Do not create a generalized
synchronization or dependency framework.

## Out Of Scope

- skill-quality scoring, thresholds, counters, or dashboards;
- transcript or history databases;
- automatic skill edits or refinement;
- generic routers, dependency graphs, recursive loading, or composition
  registries;
- custom permission frameworks;
- speculative copies for every audience.
