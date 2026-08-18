---
name: work-skill-refinement
description: Help the user design, refine, and evaluate skills and operating boundaries for a low-reasoning work AI from observed behavior. Use when deciding what the work AI should be allowed to do, converting work-agent failures into durable guidance, or deciding whether a proposed task should be delegated to the work AI at all.
---

# Work Skill Refinement

Use this ChatGPT capability to help the user refine work-side AI skills and
operating boundaries from actual work-agent behavior.

The work environment is the dogfood environment. Do not reconstruct or maintain
a copy of its skills, instructions, permissions, configuration, or other
work-specific definitions in this repository.

## ChatGPT Interaction

When this skill activates, announce `Skill: work-skill-refinement`, or announce
the full responsibility-ordered skill chain when another skill materially
applies.

Read `references/lessons.md` before advising. Treat its established lessons as
the current default assumptions for the work AI until new work evidence changes
them.

## Use Current Work Evidence

The source of truth for the current work environment is what the user provides
from the work session: current skill text, instructions, command output, observed
behavior, or other directly reported evidence.

Do not infer the current deployed work configuration from old repository files,
prior versions, or memory. If a recommendation depends on exact current work
content that has not been provided, keep the recommendation at the behavioral or
structural level until the user supplies that content.

## Capability Suitability Gate

Before helping delegate a task to the work AI, judge whether the task fits the
capabilities established in `references/lessons.md`.

Currently appropriate uses include:

- reading substantial instructions and documentation;
- invoking already-proven skills and deterministic helpers;
- running bounded commands for inspection, testing, or verification;
- writing small support scripts when their behavior can be independently
  inspected and verified before they are trusted.

Prefer deterministic scripts over prose execution flows whenever the mechanics
can reasonably be encoded. The work AI should select or invoke the proven
mechanism and interpret its output rather than repeatedly reconstructing a
multi-step operational procedure from instructions.

Do not recommend delegating artifacts whose future value depends on trustworthy
reasoning or maintainable authorship. In particular, stop and advise against
using the work AI to author:

- durable or authoritative documentation;
- reusable skills;
- production or scalable code that later work will build upon;
- scripts larger than roughly a couple hundred lines;
- architectural or refactoring work that depends on sustained reasoning across
  many decisions.

When the user proposes one of these tasks for the work AI, say that it exceeds
the currently proven capability boundary and recommend using the user, ChatGPT,
or another stronger development model instead. Do not try to solve a known
capability mismatch by making the prompt longer or more elaborate.

## Separate Reading From Authorship

The work AI has demonstrated that it can consume substantial instructions and
documentation. Do not generalize that success into trust in authorship.

Reading an authoritative source, selecting a proven skill, interpreting bounded
output, and explaining a failure are materially different from producing a new
artifact that must remain correct and maintainable over time.

## Refine Empirically

When the user reports new work-agent behavior:

1. identify what was actually attempted;
2. identify the observed success or failure;
3. decide whether it changes the known capability boundary;
4. recommend the smallest work-side instruction, skill, script, or permission
   change that addresses the demonstrated behavior;
5. have the user dogfood that change at work;
6. promote a conclusion into `references/lessons.md` only when the evidence is
   strong enough that the user wants it treated as an established lesson.

Do not predict every possible failure and harden against it in advance. The
model's failure modes are difficult to predict; observed behavior should drive
constraints.

## Durable Lessons

`references/lessons.md` is the durable record for generalized work-agent
conclusions.

Keep it small. Record lessons, not transcripts. Separate established conclusions
from ideas still under evaluation. Preserve enough rationale to understand why a
constraint exists without storing work-specific implementation details.

Because this repository is public, never record proprietary work information,
internal names, sensitive paths, credentials, infrastructure details, pasted
work instructions, or other non-public material in the lessons file.

## Repository Boundary

This repository may contain the ChatGPT guidance for how to reason about the
work AI and the generalized lessons learned from dogfooding it.

It must not become the deployment source for the work AI's actual skills or
configuration. Work-side definitions are created, changed, and tested while the
user is at work.
