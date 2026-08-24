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
chain when other skills are materially active. Briefly summarize that the review
will evaluate activation, guidance sufficiency, audience placement,
composition, helper boundaries, permissions, dogfooding, and whether no action
is correct before continuing.

Begin follow-up responses with the active skill or chain without repeating the
summary unless the invocation or chain changes materially.

## Evidence Before Judgment

Use the smallest evidence surface that supports the decision:

- the skill's activation description and canonical instructions;
- materially used skills and observed activation or omission;
- corrections, interventions, repeated approaches, rediscovery, permission
  friction, scope drift, and validation results;
- tasking, repository, and provider-native execution constraints;
- the active ticket's `Skill Candidates` and `Skills` entries when the judgment
  originates from checkpoint or promotion review;
- behavioral dogfooding of positive activation, negative boundaries, and
  intended composition.

Skill use alone is exposure, not evidence of deficiency. A single isolated agent
mistake is not enough to change a skill unless it reveals a durable failure
class or a safety invariant was violated.

## Skill Candidates And Accepted Skills

A `Skill Candidates` entry is a reviewed hypothesis that observed process
friction may justify reusable skill investment. It is not yet authorization to
create or modify a skill.

When asked to adjudicate a candidate, return the smallest correct disposition:

- **keep candidate** when the evidence is still materially inconclusive;
- **no skill** when the evidence is isolated, already covered by sufficient
  guidance, better explained by tasking/provider/tooling/executor behavior, or
  not reusable enough to justify a skill change;
- **refine existing skill** when an existing skill owns the behavior and its
  activation or guidance is the responsible deficiency;
- **create skill** when repeated work, durable operational knowledge, recurring
  friction, or a demonstrated invariant has no correct existing owner and a new
  reusable capability would materially improve execution.

Do not use mechanical evidence thresholds such as "three observations means a
skill." Judge recurrence, cost, correctness risk, portability, and reuse value.
One severe recurring class may justify immediate action; many small annoyances
may still justify no skill.

When a candidate is accepted for `create skill` or `refine existing skill`, move
it from `Skill Candidates` into the active ticket's `Skills` section. Preserve
only enough accounting context to identify the originating checkpoint/friction,
the accepted action and owner, implementation status, and dogfood result. Do not
copy the full investigation or canonical skill text into the ticket.

At ticket promotion/closure, every unresolved candidate requires a final
skill-authoring disposition. A candidate cannot remain merely "reassess later"
at issue completion.

## Failure Classification

Distinguish before recommending a change:

- wrong skill activated;
- needed skill failed to activate;
- correct skill activated but guidance was insufficient;
- guidance was sufficient but the executor failed to follow it;
- tasking or provider instructions were deficient;
- tooling or deterministic helper capability was deficient;
- isolated, non-actionable agent error.

For wrong or missing activation, identify the smallest responsible owner before
recommending a skill edit. Check, in order as applicable, the canonical skill's
activation metadata or instructions, derivative discovery surfaces such as
`skills/index.md`, caller or provider instructions, and composition/discovery
behavior. Change only the responsible owner: a correct canonical skill should
not be modified to compensate for a discovery-surface, caller-instruction,
tooling, or composition defect.

Only the skill-definition cases justify a skill change. Otherwise recommend no
action or the responsible tasking, instruction, permission, tooling, or
execution correction.

## When To Create, Refine, Or Leave Unchanged

Create or refine a skill when repeated work, durable operational knowledge,
recurring friction, a demonstrated safety/correctness invariant, or strong
behavioral evidence shows that reusable guidance would materially improve future
execution. Keep the guidance proportional to evidence and risk.

`no action` is valid when the evidence is isolated, ambiguous, task-specific,
adequately handled by existing guidance, or better explained by non-skill
failure. Do not preserve obsolete rules or create a skill because an abstraction
is merely imaginable.

## Work Accepted Skill Investments Immediately

An accepted skill investment discovered at a named product checkpoint is
checkpoint-review remediation, not backlog decoration. Complete the accepted
create/refine work before advancing to the next named product checkpoint unless
a real permission, scope, dependency, or owning-repository blocker requires
explicit escalation.

Dogfood the resulting skill or refinement against the friction that justified it
while that evidence is still available. The dogfood need not replay an expensive
production event when a cheaper faithful reproduction proves the behavior, but
it must exercise the claimed improvement rather than merely checking syntax.

If the accepted skill is owned in another repository, make the change in its
canonical owning repository and record the resulting change reference and
dogfood outcome in the originating ticket's `Skills` entry. Do not create a
project-local duplicate solely to avoid the repository boundary.

## Audience And Placement

Use the current audience model:

```text
skills/<name>/                 # genuinely shared operational behavior
skills/chatgpt/<name>/         # ChatGPT judgment or interaction behavior
skills/copilot/<name>/         # Copilot repository execution behavior
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

A helper deficiency discovered during candidate review can justify helper work
owned by an existing or newly accepted skill without implying that helper logic
belongs in the ticket itself. Record the accepted skill investment in the ticket
and keep deterministic mechanics with the canonical skill package.

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
