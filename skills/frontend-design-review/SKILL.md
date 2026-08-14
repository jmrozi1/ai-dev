---
name: frontend-design-review
description: Review an existing front-end interface for unnecessary cognitive load, weak action hierarchy, redundant visual containment, and failure to prioritize the normal happy path. Use when evaluating screenshots, implemented UI, mockups, or proposed front-end designs.
---

# Front-End Design Review

Use this skill to review an existing front-end design for clarity, hierarchy, and efficiency.

The goal is not to redesign the application from scratch. Evaluate whether the current design is balanced across the principles below, then recommend only the smallest changes that materially improve it.

## Core Principle

Visual prominence should track task frequency and importance.

Every visible control, label, border, container, instruction, and decorative element consumes screen space and attention. Each should earn its place.

Optimize first for the repeated happy path: what the user normally came to this screen to do when nothing is wrong.

## Design Metrics

Evaluate the screen independently through every metric below. For each metric, temporarily treat that tradeoff as the primary lens before moving to the next one.

### 1. Visible Actions

**Balance:** immediate access to useful actions vs persistent visual and cognitive cost.

Keep the dominant workflow immediately available. Move infrequent configuration or secondary operations behind appropriate affordances when the extra interaction costs less than keeping them visible all the time.

Do not hide rare actions when they are critical, urgent, or need strong discoverability.

### 2. Action Hierarchy

**Balance:** prominence of an action vs its frequency and importance in the current context.

Primary actions should receive more visual weight than secondary, exceptional, or destructive actions. Navigation state should communicate location without impersonating a primary action.

The exact styling is less important than whether the hierarchy matches the workflow.

### 3. Containment

**Balance:** clarity of grouping vs visual noise and redundant boundaries.

Logical nesting does not require visual nesting. Borders, cards, shadows, and rounded containers should communicate a meaningful boundary.

Prefer lighter hierarchy such as whitespace, alignment, typography, proximity, indentation, subtle backgrounds, or dividers when those are sufficient.

Avoid bubbles within bubbles when several treatments are doing the same grouping job.

### 4. Happy-Path Prominence

**Balance:** guidance for exceptional states vs keeping normal successful usage dominant.

Do not make users repeatedly move past onboarding, warnings, disclaimers, helper text, configuration details, or edge-case guidance when those are not relevant to the current state.

Errors should become prominent when errors exist. Guidance should become prominent when guidance is needed.

### 5. Persistent Text

**Balance:** useful context vs recurring reading cost and screen-space cost.

Ask whether each persistent label or explanation changes what the user will understand or do now. Remove text that merely restates nearby controls or behavior the user will quickly learn.

Onboarding may be useful. Permanent onboarding usually becomes clutter.

### 6. Repeated Collection Density

**Balance:** density and scanability vs readability, touch-target size, grouping, and access to important row actions.

Repeated content should use as little vertical space per item as the information allows. Prefer a single row per item when practical.

Judge the collection at realistic scale, not only with a few examples. Ask whether the treatment would still be efficient with roughly thirty items.

Review cumulative cost across:

- cell padding
- borders or dividers
- margins
- inter-row gaps
- repeated labels or metadata
- repeated row actions

Do not automatically use both containment and spacing to separate every row. If one treatment already establishes the boundary, additional separation should justify its cumulative cost.

Preserve immediate access to the collection's primary row action. Secondary operations may use compact icons, menus, or other lower-cost affordances when appropriate.

### 7. Recoverability

**Balance:** interruption of intentional actions vs the cost and reversibility of mistakes.

Prefer reversible operations, undo, or restoration over confirmation prompts when practical.

Do not force every intentional action to pay for the possibility of an accidental one unless the consequences justify the interruption.

### 8. Contextual Interaction Cost

**Balance:** persistent UI cost vs the cost of an extra interaction in the current context.

Do not optimize for minimum click count in isolation. An operation that belongs behind a menu during execution may deserve a visible control during configuration.

Separate setup from tuning. Distinguish decisions needed to establish a usable result from decisions that merely tune or override sensible defaults. Give setup decisions immediate prominence; when tuning is infrequently needed, weigh its discoverability against the recurring cognitive cost of presenting it alongside core setup.

Choose the interaction structure that minimizes total cognitive cost for the current mode and workflow.

### 9. State-Dependent Controls

**Balance:** continuity and discoverability vs accumulating controls as state changes.

When a state transition makes an existing control obsolete, prefer replacing or repurposing that space for the next relevant action instead of adding another persistent control.

Do not overload one control with unrelated meanings merely to save space.

## Review Process

Before recommendations, identify:

- the screen's dominant user task
- the normal happy-path state

Then evaluate every Design Metric independently.

For each metric:

1. Identify the competing costs described by that metric.
2. Judge where the current design lands after balancing those costs.
3. Record one of: `Balanced`, `Needs adjustment`, or `N/A`.
4. Give one or two sentences explaining the tradeoff and conclusion.
5. Do not let a finding from an earlier metric substitute for evaluating the current one.

Only after every metric has been evaluated:

1. Combine overlapping findings.
2. Prioritize the resulting material issues.
3. Recommend the smallest reasonable set of design changes.

If implementation guidance is requested, derive the plan strictly from the accepted recommendations. Do not introduce new redesign ideas during implementation planning.

## Report Format

Start with a concise metric report:

| Metric | Result | Reasoning |
|---|---|---|
| Visible Actions | Balanced / Needs adjustment / N/A | Brief tradeoff reasoning |
| Action Hierarchy | ... | ... |
| Containment | ... | ... |
| Happy-Path Prominence | ... | ... |
| Persistent Text | ... | ... |
| Repeated Collection Density | ... | ... |
| Recoverability | ... | ... |
| Contextual Interaction Cost | ... | ... |
| State-Dependent Controls | ... | ... |

Then provide `Recommended changes` containing only material changes after overlapping findings have been combined.

A short recommendation list is good. A short metric report is not permission to skip metrics.

Preserve existing behavior unless changing it is necessary to resolve the design problem.

## ChatGPT Interaction

When ChatGPT begins a substantive design review, begin with the active skill or responsibility-ordered composed chain. For standalone use, announce:

`Skill: frontend-design-review`

When other materially active skills own distinct responsibilities, list every one in the chain rather than announcing this skill alone.

Recommend an advisory reasoning level, briefly summarize that the review will evaluate hierarchy, interaction cost, recoverability, density, happy-path prominence, and material design issues, then ask `Proceed?` and stop before substantial analysis until confirmation.

The recommendation is advisory and does not change or assume the actual ChatGPT reasoning setting. This instruction is scoped to ChatGPT use and does not alter Copilot or Work behavior.

The gate is activation-time and occurs once per continuous design review. After the user proceeds, begin follow-up responses with the active skill without repeating the reasoning cue, design summary, or proceed gate. Gate again only for a new invocation, a materially changed skill chain, or a scope change that makes a new reasoning decision meaningful.
