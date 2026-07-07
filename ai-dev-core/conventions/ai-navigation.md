# AI Navigation Convention

AI navigation starts from summaries and ends at source.

Generated summaries are routing aids. They are not final authority.

## Navigation Order

Use this order when answering a repository question:

1. Read `.ai-dev.yaml` for documentation layout.
2. Read `ai-docs/architecture-summary.md` when present.
3. Use the architecture summary to choose relevant directory `summary.md` files.
4. Read the selected directory summaries to identify likely source files.
5. Use `ai-docs/dependency-map.md` when the question involves implicit relationships.
6. Read the minimal source files needed to answer the question.
7. Verify exact behavior against source.

## Architecture Summary

Use `architecture-summary.md` to decide which included source directories are likely relevant.

The architecture summary should route to directory `summary.md` files, not directly to source files.

It should be compact and table-like when possible.

It should not list individual source files or identify key files.

Its directory set should come from configured source discovery, not a raw filesystem walk.

If `architecture-summary.md` is missing, fall back to discovered `summary.md` files and report that top-level architecture routing is unavailable.

## Summary Files

Use `summary.md` files to decide which source files are likely relevant.

A good summary answers:

```text
Should AI read this source file for the current task?
```

It should not try to answer exact implementation questions by itself.

## Dependency Maps

Use `dependency-map.md` for questions involving:

- load order
- manifests or `.toc` files
- globals or shared namespaces
- producer/consumer relationships
- config registries
- event or lifecycle wiring
- plugin systems
- extension points

If a dependency map is missing and the question requires dependency context, search source directly and state that no generated dependency map was available.

## Source Verification

Always verify against source when the answer depends on exact behavior, including:

- current values
- parameter names
- config entries
- spell names
- row order
- coordinates
- durations
- thresholds
- conditionals
- side effects
- load order
- event handling
- function bodies
- security-sensitive behavior

If generated summaries conflict with source, source wins.

## Discoverability Issues

Report a discoverability issue when summaries do not route clearly to the source that answered the question.

Use wording like:

```text
The answer was found in source, but the current summary did not clearly route to that file.
```

## Success Criteria

AI navigation is successful when AI can start from a natural-language question, use summaries to identify likely source files, verify against source, and answer without reading the entire project by default.
