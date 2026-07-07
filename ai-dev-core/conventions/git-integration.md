# Git Integration Convention

Git integration helps identify changed source files and determine whether routing documentation needs updates.

The goal is not to keep generated documentation synchronized with every source value.

The goal is to keep summaries and dependency maps useful for AI routing.

## Changed Source Files

For each changed source file, determine the matching summary file:

```text
<root source file>
  -> ai-docs/summary.md

<dir>/<source file>
  -> ai-docs/<dir>/summary.md
```

Review whether the summary entry still describes the file role and routing reasons accurately.

## When Summary Updates Are Needed

Update a summary when:

- a new source file should be routable
- a source file was deleted but remains in a summary
- a source file moved or was renamed
- a file's ownership or role changed
- a summary routes questions to the wrong file
- a summary is too vague to support routing

Do not update a summary merely because exact source values changed.

Examples that usually do not require summary updates:

- cooldown changed
- threshold changed
- table entry changed
- row order changed
- implementation details changed

## Dependency Map Updates

Update `dependency-map.md` when changed files affect project-level implicit relationships such as:

- load order
- manifest inclusion
- global namespace creation
- shared registry population
- producer/consumer relationships
- lifecycle wiring
- plugin/extension-point wiring

Do not track every generic utility usage.

## Rename and Delete Handling

For renames:

- update summary entries from old path to new path
- preserve useful routing text if the file role did not change
- update dependency-map references when relevant

For deletes:

- remove stale summary entries
- remove dependency-map references when relevant

## Review Output

Review findings should distinguish:

- missing summary
- stale summary
- weak summary routing
- dependency-map update needed
- no documentation change needed
- uncertainty

A valid review result may be:

```text
No documentation changes required.
```

when source changes do not affect routing.
