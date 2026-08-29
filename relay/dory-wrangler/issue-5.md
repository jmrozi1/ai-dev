# Dory-Wrangler Issue #5 — Build the attention workspace and proactive Dory console

> Transport snapshot only. Canonical source: https://github.com/jmrozi1/dory-wrangler/issues/5
>
> Target repository: `~/dory-wrangler`. This file is not an AI Dev ticket or authorization to modify AI Dev.
>
> A human instruction must select the checkpoint to execute.

## Checkpoints

1. Build the desktop-first shell with the two-pane attention workspace above a persistent bottom Dory console, and connect both interaction surfaces to their APIs.
2. Implement clickable Waiting, Running, and Disconnected count filters, with only Waiting enabled by default.
3. Implement the minimal oldest-first attention list.
4. Implement the right-side decision explanation, free-form response field, and Send action.
5. Implement the bottom console for starting or resuming a logical Dory intent conversation without first selecting an attention item.
6. Add collapsed Details for evidence and output plus Enter-to-send and Shift+Enter-for-newline behavior.
7. Verify loading, empty, stale, disconnected, AI-unavailable, error, and successful-response states end to end.

## Acceptance Criteria

- The upper left pane centers only the items and states relevant to the user's attention.
- Queue rows contain subject, project or ticket, and time without extra action controls.
- The upper right pane shows one decision without repeating its title.
- The bottom console lets the user proactively submit rough intent and continue the resulting Dory conversation.
- Proactive console conversation and reactive attention decisions remain visibly distinguishable while sharing the same durable control plane.
- There are no accept/reject/context/defer buttons, inspection controls, overflow menus, blocker-type chrome, or transient success message.
- Enter sends and Shift+Enter adds a newline.
- Details remain collapsed by default and expose the underlying evidence when requested.
- The interface remains usable while other agents continue running.
- A real submitted decision response updates durable state and allows the harness to continue.
- A real console conversation can produce accepted executable intent and authorize the harness to start work through the defined readiness policy.
- When AI operations fail, a clear top-level banner reports that AI is unavailable while non-AI state remains viewable.

## Full Description

Deliver the first usable Dory-Wrangler interaction surface. The upper area operates like Coxswain at the decision level—attention on the left and the current decision on the right. The bottom console is the proactive surface where the user talks to Dory-Wrangler itself, refines new work, and starts execution.

Both surfaces are backed by Dory-Wrangler's durable state and deterministic harness. The apparent continuity of the Dory conversation must not require one permanently running agent.
