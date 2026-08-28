# Dory-Wrangler Issue #5 — Build the two-pane decision console

> Transport snapshot only. Canonical source: https://github.com/jmrozi1/dory-wrangler/issues/5
>
> Target repository: `~/dory-wrangler`. This file is not an AI Dev ticket or authorization to modify AI Dev.
>
> A human instruction must select the checkpoint to execute.

## Checkpoints

1. Build the desktop-first two-pane shell and connect it to the attention API.
2. Implement clickable Waiting, Running, and Disconnected count filters, with only Waiting enabled by default.
3. Implement the minimal oldest-first attention list.
4. Implement the right-side decision explanation, free-form response field, and Send action.
5. Add collapsed Details for evidence and output plus Enter-to-send and Shift+Enter-for-newline behavior.
6. Verify loading, empty, stale, disconnected, error, and successful-response states end to end.

## Acceptance Criteria

- The left pane centers only the items and states relevant to the user's attention.
- Queue rows contain subject, project or ticket, and time without extra action controls.
- The right pane shows one decision without repeating its title.
- There are no accept/reject/context/defer buttons, inspection controls, overflow menus, blocker-type chrome, or transient success message.
- Enter sends and Shift+Enter adds a newline.
- Details remain collapsed by default and expose the underlying evidence when requested.
- The interface remains usable while other agents continue running.
- A real submitted response updates durable state and allows the harness to continue.

## Full Description

Deliver the first usable Dory-Wrangler release. It should operate like Coxswain at the interaction level—attention on the left and the current decision on the right—while remaining entirely backed by Dory-Wrangler's durable state and deterministic harness.
