---
name: frontend-design-review
description: Review an existing front-end interface for unnecessary cognitive load, weak action hierarchy, redundant visual containment, and failure to prioritize the normal happy path. Use when evaluating screenshots, implemented UI, mockups, or proposed front-end designs.
---

# Front-End Design Review

Use this skill to review an existing front-end design for clarity, hierarchy, and efficiency.

The goal is not to redesign the application from scratch. Identify where the current interface creates unnecessary cognitive load, gives the wrong things too much visual weight, or makes the common workflow harder than it needs to be.

## Core Principle

Visual prominence should track task frequency and importance.

Every visible control, label, border, container, instruction, and decorative element consumes some combination of screen space and user attention. Each should earn its place.

Optimize first for the repeated happy path: what the user normally came to this screen to do when nothing is wrong.

## Review Principles

### 1. Minimize Visible Actions

Do not expose every available operation simply because the application supports it.

Ask:

- What does the user do on this screen most of the time?
- Which actions are required for that workflow?
- Which controls are only configuration, help, advanced behavior, or unusual cases?

Keep the dominant workflow immediately available.

Move infrequent configuration and secondary operations behind appropriate affordances such as settings menus, gear icons, overflow menus, advanced sections, or contextual controls.

Do not hide an action merely because it is rare when it is critical, urgent, or must remain easily discoverable.

### 2. Give Actions Appropriate Visual Weight

Do not present fundamentally different actions as though they are equally important.

Primary actions should usually be more visually prominent than secondary or exceptional actions through position, size, labeling, grouping, or styling.

Examples:

- A final `Save` action after completing a setup workflow may deserve a large explicit button.
- Common object operations such as edit, save, and delete may reasonably appear together as compact icons.
- Settings, help, about, and other rarely used controls should not visually compete with actions used during normal operation.

The exact control style is less important than whether the visual hierarchy accurately represents the workflow.

### 3. Avoid Redundant Containment

Logical nesting does not require visual nesting.

Watch for designs that mirror the implementation hierarchy directly:

page -> card -> section -> card -> item -> bordered container

Borders, cards, shadows, and rounded containers should communicate a meaningful boundary or grouping.

Ask of each container:

- Does this boundary help the user understand the screen?
- Does it separate meaningfully different sections?
- Would removing it make the grouping less clear?

If not, remove or weaken the boundary.

Prefer lighter hierarchy mechanisms where appropriate:

- whitespace
- alignment
- typography
- proximity
- indentation
- subtle background changes
- dividers

Avoid "bubbles within bubbles" when several borders are separating content that already clearly belongs together.

### 4. Make the Happy Path the Loudest

Review both meanings of the happy path:

- the workflow the user performs most often
- the normal state where nothing is wrong

The normal task should dominate the initial screen.

Do not make users repeatedly move past:

- introductory instructions
- disclaimers
- warnings for conditions that are not currently present
- permanent onboarding copy
- edge-case guidance
- configuration details
- verbose helper text

Errors should become prominent when an error exists.

Guidance should become prominent when guidance is needed.

Configuration should become prominent while configuration is being performed.

Do not make normal successful usage continually pay the visual cost of exceptional states.

### 5. Make Every Word Earn Its Place

Screen space is especially expensive on mobile devices.

Review persistent text with the same skepticism as controls and containers.

Ask:

- Does the user need this information right now?
- Does it change what they will do?
- Will a returning user still benefit from reading it?
- Could the same meaning be conveyed more directly?

Avoid persistent explanations of behavior the user will understand after the first few uses.

Onboarding may be useful. Permanent onboarding usually becomes clutter.

## Review Method

For each screen:

1. Identify the screen's dominant user task.
2. Identify the normal happy-path state.
3. Determine what should receive the most visual attention.
4. Review visible controls for frequency, importance, and necessity.
5. Review containers and borders for redundant hierarchy.
6. Review persistent text for unnecessary attention cost.
7. Report only findings that materially affect clarity or workflow.

Do not invent issues merely to produce a longer review.

## Output

Prioritize findings by impact.

For each finding, state:

- what is creating unnecessary cognitive load or misleading hierarchy
- why it matters during normal use
- the smallest reasonable design change that would improve it

Prefer a few strong findings over a comprehensive inventory of minor stylistic preferences.

Preserve existing behavior unless changing it is necessary to resolve the design problem.
