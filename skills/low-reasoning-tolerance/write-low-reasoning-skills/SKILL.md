---
name: write-low-reasoning-skills
description: Write or refine skills intended for models that cannot reliably infer intent or exercise strong judgment from concise instructions. Use when creating low-reasoning-tolerance skills or hardening an existing skill specifically because a weaker target model has demonstrated superficial compliance, missed intent, or unreliable judgment.
---

# Write Low-Reasoning Skills

Use this skill when writing instructions for a model whose reasoning is not reliable enough to safely infer the intended behavior from a concise goal.

The purpose is not to write generally better or more detailed skills. Low-reasoning-tolerance skills deliberately trade model discretion and capability ceiling for a more reliable minimum behavior from a weaker target model.

## Core Principle

Optimize the capability floor, not the capability ceiling.

Begin with the least restrictive instruction that accurately expresses the desired outcome. Add restrictions only when a demonstrated target-model failure, a non-negotiable requirement, or a safety/correctness invariant justifies them.

Do not add constraints merely because they sound helpful, make output more uniform, or anticipate hypothetical failures.

## Development Method

Use an empirical refinement loop:

1. State the desired outcome as simply as the target model may reasonably understand it.
2. Run the skill against the actual target model and a representative task.
3. Observe concrete unacceptable behavior.
4. Identify why the current instruction permitted that behavior.
5. Add the smallest instruction that excludes the demonstrated failure class.
6. Retest the target model.
7. Repeat only when another unacceptable behavior is observed.
8. Stop when the target model produces reliably acceptable behavior.

Do not optimize for excellent output if acceptable reliable output is sufficient for the target environment.

## Preserve Judgment Until It Fails

Prefer outcome-oriented instructions while the target model can follow them.

For example, prefer:

> Write an opening scope statement that allows the reader to answer, "Am I on the right page?"

Do not immediately replace that with a structural checklist of required fields unless the target model demonstrates that the outcome-oriented instruction is insufficient.

Every added structural requirement narrows the model's solution space. That may be necessary for a weak model, but it can also prevent a stronger model from producing a better result.

## Constrain Observed Failure Classes

When the target model fails, correct the class of failure rather than overfitting to one bad output.

If a model responds to a scope requirement with:

> You are on the right page.

Do not prescribe one exact replacement sentence or arbitrarily require a list of specific fields unless that is actually necessary.

Instead, identify the failure:

- the model asserted applicability rather than providing information from which the reader could determine applicability.

A proportional constraint might be:

> Answer through descriptive information about the document's applicability; do not merely assert that the reader is on the correct page.

Retest before adding anything else.

## Prefer Outcome Tests Over Prescribed Implementations

When possible, define an observable result rather than the exact form used to produce it.

Prefer:

> A reader following the documented route must be able to identify the exact next document or section without searching the page, inferring unexplained terminology, or already knowing the documentation structure.

Over prematurely prescribing:

- exact heading names;
- exact sentence counts;
- mandatory wording;
- arbitrary field lists;
- fixed document structures.

Move toward mechanical requirements only when the weaker model has demonstrated that it cannot reliably satisfy the outcome test without them.

## Avoid Checklist Substitution

Do not convert a meaningful outcome into a checklist unless the checklist actually protects that outcome.

Weak models may satisfy structural requirements while still failing the real task. A skill that requires a scope heading, routing statement, table of contents, and prerequisite section may still produce unusable documentation if the model learns only to detect or generate those shapes.

Whenever adding a mechanical rule, ask:

- What observed failure does this rule prevent?
- Can the model satisfy the rule while still reproducing the original failure?
- Is there a less restrictive rule that would prevent the same failure?

If the rule does not materially protect the intended outcome, do not add it.

## Use Examples Carefully

Examples strongly influence weak models and can become accidental templates.

Do not add examples merely to make a skill feel complete.

Use examples when the target model has demonstrated that a distinction is not understood from instructions alone. Prefer contrasting pass/fail examples that explain the behavioral distinction without prescribing unnecessary wording or structure.

For example:

> Fail: "You are on the right page."
>
> Reason: It asserts applicability without giving the reader information needed to determine applicability.
>
> Pass: "Use this guide to install Kubernetes on supported high-side environments."
>
> Reason: It identifies the task and environment to which the page applies.

Do not require future outputs to imitate the passing example unless exact form is itself a requirement.

## Preserve Proven Constraints

Low-reasoning-tolerance skills may contain instructions that appear awkward, redundant, or overly specific when read by a stronger model.

Do not remove such constraints merely because a stronger model would perform better without them. First determine why the constraint exists and whether removing or weakening it reintroduces a demonstrated failure.

During skill development, retain enough test or working context to understand the origin of unusual constraints, for example:

```text
Constraint: Do not merely assert that the reader is on the correct page.
Origin: Target model accepted "You are on the right page" as a valid scope statement.
```

This failure history does not need to remain as prose in the deployed `SKILL.md` when tests or other development artifacts preserve it adequately. The important requirement is that proven constraints are not casually removed after their purpose is forgotten.

## Do Not Speculatively Harden

Do not predict every possible way a weak model might fail and encode preventive rules in advance.

Speculative hardening creates several risks:

- lowering the quality ceiling unnecessarily;
- making the skill longer and harder for the weak model to follow;
- creating conflicting instructions;
- encouraging superficial checklist compliance;
- obscuring which constraints are actually necessary.

Observed friction should drive refinement.

## Acceptance Standard

The stopping condition is reliable acceptable behavior from the target model on representative tasks.

Do not continue adding restrictions merely to make output more polished, deterministic, or similar to what a stronger model would produce.

If the target model cannot meet the acceptable floor without highly mechanical instructions, use those instructions. That tradeoff is the purpose of this category.

If a constraint later proves unnecessary across representative weak-model tests, it may be simplified or removed. Verify the behavior before doing so.

## Output When Designing a Low-Reasoning Skill

When proposing or refining a low-reasoning-tolerance skill:

- state the intended outcome;
- identify the target model behavior that currently fails, when known;
- distinguish hard requirements from constraints added because of observed model failures;
- recommend the smallest next instruction change;
- avoid adding speculative restrictions;
- identify what should be retested on the target model.

When no weak-model failure has yet been observed, prefer a minimal first version rather than inventing a hardened one from theory.
