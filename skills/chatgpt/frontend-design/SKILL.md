---
name: frontend-design
description: Design or materially restructure a front-end screen or interaction flow before implementation. Use when deciding information hierarchy, visible controls, navigation, state transitions, responsibility boundaries, or other structural UI behavior; prefer cheap ASCII/text prototypes when those decisions are still unresolved. Do not use for routine implementation of an already-settled design or for rendered visual review.
---

# Front-End Design

Use this skill to resolve material front-end structure before implementation.

The goal is to discover interaction and information-model problems while they are
still cheap to change. Do not spend implementation and screenshot cycles learning
that the screen itself was organized around the wrong model.

## Classify The Uncertainty

Before delegating implementation, decide whether the important uncertainty is
primarily structural or primarily visual.

Structural uncertainty includes questions such as:

- which objects or information belong on the screen;
- which actions remain visible and which are secondary;
- whether a workflow is one screen or several;
- navigation and selection structure;
- state transitions and what appears after an action;
- responsibility boundaries between screens or modes;
- whether two controls can become one interaction;
- setup versus tuning or progressive-disclosure boundaries.

Visual uncertainty includes questions such as spacing, color, typography,
containment weight, touch-target feel, responsive fit, and whether one element is
visually too loud or too quiet.

When the uncertainty is primarily visual, do not force an ASCII phase. A rendered
interface or mockup is usually the cheaper and more informative artifact, and
`frontend-design-review` owns substantive rendered-design evaluation.

## Prototype Structural Decisions Cheaply

When material structural uncertainty exists, create a low-fidelity ASCII or text
prototype before implementation is delegated.

Keep the prototype intentionally cheap. It should show only enough to make the
important structure and interactions discussable. Prefer simple text layout over
ASCII artwork.

Represent the important states needed to settle the design, for example:

```text
Routines

[ --select routine-- ]

[M] [T] [W] [T] [F] [S] [S]

[ --add exercise-- ]

Bench Press                         ⋮
Cable Row                           ⋮
```

When behavior matters, show the transition as well as the static state:

```text
select exercise
-> add it to the current day
-> reset to --add exercise--
```

Do not enumerate every theoretical state. Include only states or transitions
whose differences could materially change the implementation or user workflow.

## Resolve Before Implementation

Use the low-fidelity prototype to settle material questions about:

- information hierarchy;
- screen and responsibility boundaries;
- visible controls;
- navigation and selection;
- state transitions;
- normal happy-path interaction;
- removal of unnecessary interactions or redundant controls.

Iterate on the cheap representation until the material structure is agreed.
Do not delegate implementation while a known structural question could still
change the screen or interaction model substantially.

This is not a requirement to eliminate every implementation choice. Leave
framework, DOM structure, styling details, and other non-material implementation
choices open when they do not require inventing product intent.

## Compose With Specific Interaction Skills

When the design uses an interaction with its own reusable capability, load that
capability rather than re-specifying its contract here. For example,
`search-select` owns browse-first searchable single-select behavior.

This skill owns the screen or flow structure around those interactions. It does
not absorb component-specific behavior merely because the component appears in
the design.

## Implementation Handoff

Once the structural design is agreed:

- derive implementation instructions strictly from the accepted structure and
  relevant capability contracts;
- do not introduce a new redesign during delegation;
- keep the implementation slice bounded to the agreed behavior;
- use rendered UI evidence afterward for visual hierarchy, spacing, density,
  containment, touch targets, responsive behavior, and styling decisions.

If rendered evidence reveals a new structural problem, return to a cheap
structural representation before beginning another expensive implementation
loop.

## Out Of Scope

This skill does not own:

- rendered visual review metrics;
- color, typography, spacing, or branding systems;
- framework or component-library architecture;
- implementation mechanics for an already-settled design;
- exhaustive wireframes or high-fidelity mockups when a cheaper representation
  answers the structural question;
- speculative UI states that have no current product need.

## ChatGPT Interaction

When ChatGPT intentionally activates this skill, begin the user-facing response
with `Skill: frontend-design`, or include it in the responsibility-ordered
composed chain when other skills own materially distinct responsibilities.

When material structural uncertainty is detected, make the pre-implementation
boundary observable: use a cheap ASCII/text prototype and resolve the material
structure before delegating implementation. Do not add a separate ceremony or
gate when the user has already asked to design the interface; agreement on the
prototype is the design checkpoint.
