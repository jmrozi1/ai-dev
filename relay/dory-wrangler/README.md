# Dory-Wrangler relay

This directory is a temporary, one-way transport for Dory-Wrangler issue snapshots into the internal network where AI Dev is mirrored.

## Authority and scope

- The canonical tickets are the GitHub issues in `jmrozi1/dory-wrangler`.
- These files are transport snapshots. They are not AI Dev tickets, rails, tasking state, or authorization to modify the AI Dev product.
- The target working repository is `~/dory-wrangler` on the internal VM.
- A human instruction must select the issue and checkpoint to execute. Reading a snapshot alone does not authorize work.
- Work must remain inside the selected Dory-Wrangler checkpoint unless the human explicitly changes the assignment.
- Executor evidence returns through the human relay; these files are not an append-only execution log.

## Updating snapshots

Replace a snapshot from its canonical GitHub issue when the issue changes. Do not append history or treat the snapshot as more current than its source URL.

This relay should be removed or frozen when Dory-Wrangler can receive and manage its own internal assignments.
