# Generate Docs Workflows

Workflow instructions for generating AI routing summaries.

The current documentation model uses:

- `architecture-summary.md` as the top-level directory routing map
- directory-level `summary.md` files for source-file routing
- optional `dependency-map.md` files for implicit dependency relationships such as load order, globals, producers, and consumers
- source files as the final authority for exact behavior

Generated summaries are routing aids. Architecture summaries route to directories; directory summaries route to source files. They do not replace source.
