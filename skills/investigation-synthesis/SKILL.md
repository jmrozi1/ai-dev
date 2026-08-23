---
name: investigation-synthesis
description: Preserve the durable results of substantial investigation without turning research, debugging, or reverse engineering into transcript documentation. Use when accepted findings, source interpretations, disproven assumptions, implementation boundaries, or evidence would be costly or risky to rediscover.
---

# Investigation Synthesis

Turn substantial investigation into the smallest durable knowledge surface that
lets future work continue without materially repeating the discovery.

The goal is not to document the investigation. The goal is to preserve what was
learned that still matters.

## When To Use This Skill

Use this skill when research, debugging, reverse engineering, source comparison,
legacy-system discovery, production diagnosis, or implementation planning has
produced material knowledge whose loss would create meaningful rediscovery,
correctness risk, or scope confusion.

Typical signals include:

- a non-obvious architectural or protocol fact;
- a source/data interpretation future work depends on;
- a disproven assumption likely to recur;
- a material implementation or scope boundary discovered through evidence;
- a reusable diagnostic or research technique;
- a settled decision whose rationale depends on investigation evidence;
- a large evidence artifact whose loss would require substantial reconstruction;
- conflicting or historical sources that required version/fidelity adjudication.

Do not activate merely because files were inspected, a routine question was
answered, or an isolated typo/debugging mistake was resolved. Cheap facts that
are obvious from the current code and inexpensive to rediscover usually need no
durable synthesis.

## Preserve Conclusions, Not The Transcript

Keep only the durable result of the investigation.

Good candidates include:

- accepted facts and their important scope/version qualifiers;
- confirmed implementation boundaries;
- source hierarchy or interpretation rules;
- disproven assumptions that would otherwise cause repeated wasted work;
- unresolved questions that materially block or constrain later execution;
- accepted formulas, mappings, identifiers, or data relationships;
- the minimum provenance needed to understand why a non-obvious conclusion is
  trustworthy.

Usually discard:

- command transcripts;
- search history;
- failed syntax and dead-end queries;
- intermediate hypotheses that no longer affect current work;
- superseded matrices or scratch summaries;
- broad log dumps;
- agent conversation history;
- evidence that is trivial to regenerate and has no independent reuse value.

Do not preserve investigation chronology unless the chronology itself is
material to correctness.

## Reconcile Evidence Before Promoting It

A polished source is not automatically a current or authoritative source.
Before promoting an investigated conclusion into durable knowledge, check the
material dimensions that could invalidate it, such as:

- product/version/build/era;
- current code versus historical behavior;
- direct production data versus secondary summaries;
- stock/reference behavior versus project-local behavior;
- confirmed fact versus inference;
- whether newer evidence supersedes older documentation.

When sources conflict, do not silently choose the most convenient one. Resolve
the conflict at the fidelity level required by the work, or preserve the
uncertainty explicitly.

Historical text embedded in a current page remains historical evidence. Do not
promote it as current behavior merely because the page itself is current.

## Route Each Finding To The Smallest Correct Home

Classify material findings by where they will actually be reused.

### Ticket-specific durable intent

Use the active ticket when the finding matters to this work slice: accepted
requirements, implementation boundaries, source/fidelity decisions, current
checkpoints, focused unresolved implementation facts, or evidence expectations.

If the ticket already contains everything a fresh competent executor needs,
that is a successful synthesis outcome. Do not create another document merely
because substantial research occurred.

### Reusable project knowledge

Use the project's existing knowledge surface when a non-obvious fact will
materially help future work outside the current ticket. Prefer an existing
knowledgebase/topic file over creating a parallel documentation taxonomy.

Examples include stable protocol findings, architecture boundaries, data-source
semantics, recurring environment behavior, and reusable diagnostics.

### Durable structured evidence

Preserve a data artifact when the evidence itself has continuing value and is
too large or structured for a ticket or knowledge article, such as a canonical
matrix, source inventory, fixture, or generated comparison used by later
implementation or validation.

Do not preserve large data solely because generating it was expensive if the
accepted conclusions are sufficient for future work.

### Regeneration contract

Prefer a compact regeneration contract over storing derived output when the
evidence can be deterministically reproduced. Preserve only what a fresh agent
needs to regenerate the accepted artifact, such as source/version pins,
selection/filter rules, transformation logic, and accepted interpretation.

Do not let TEMP files become canonical implementation dependencies.

### Discard

Explicitly discard findings and artifacts that are superseded, trivial,
transient, duplicated elsewhere, or unlikely to reduce meaningful future work.
Discard is an intentional outcome, not a failure to document.

## Preserve Proportional Provenance

Keep enough provenance for a future reader to distinguish evidence from
assertion, but do not turn synthesis into a bibliography project.

For non-obvious or version-sensitive conclusions, useful provenance may include:

- repository path plus function/type/table name;
- build, version, revision, or commit pin;
- authoritative data table or external reference;
- concise reason one source supersedes or constrains another;
- confidence or unresolved status when evidence remains incomplete.

Do not paste large source excerpts when a precise pointer and concise conclusion
are sufficient.

## Separate Current Intent From Reusable Knowledge

A ticket describes the current work slice. A knowledgebase describes facts
expected to outlive that slice. A tasking file describes current executor state.
Do not use one merely because it is convenient when another is the correct
lifetime boundary.

When a finding changes requirements or scope, requirements-driven-development
owns expressing the resulting intent. When a finding changes current execution
state, the orchestrator owns the ticket/tasking transition. This skill owns the
judgment about which investigation results deserve durable promotion and where
those results belong.

## Respect Write Authority

Investigation synthesis does not itself grant permission to mutate tickets,
repository documentation, generated artifacts, or external systems.

When the current assignment is read-only, return the classification and proposed
durable destinations without writing them. When writes are authorized, update
only the smallest necessary durable surfaces and avoid unrelated cleanup.

## Cold-Start Completion Test

A substantial investigation is not complete while material accepted knowledge
exists only in chat, TEMP files, logs, terminal scrollback, or one agent's
context.

Before declaring synthesis complete, ask:

> Could a fresh competent agent continue this work without materially repeating
> the investigation?

If yes, stop. More documentation is not automatically better.

If no, identify the missing durable knowledge and place it in the smallest
correct home.

Do not require perfect preservation. Preserve enough to avoid meaningful
rediscovery, incorrect re-litigation, or loss of implementation-critical
context.

## Suggested Synthesis Shape

For a substantial investigation handoff, a compact classification is usually
enough:

- **Ticket-specific durable intent:** what belongs in the current ticket.
- **Reusable project knowledge:** what should survive beyond this ticket.
- **Durable evidence / regeneration:** what structured evidence must survive and
  whether to store or regenerate it.
- **Discard:** what should intentionally disappear.
- **Remaining uncertainty:** only unresolved facts that can still change the
  implementation route or acceptance evidence.

Do not force empty categories into the output.

## Out Of Scope

- transcript or research-history archives;
- generic documentation generation;
- knowledge graphs, provenance databases, or traceability systems;
- automatic promotion of every discovered fact;
- creating a new documentation taxonomy when the project already has one;
- preserving speculative conclusions as accepted knowledge;
- duplicating ticket intent into a knowledgebase for safety;
- turning reproducible TEMP/scratch output into canonical state by default.

## ChatGPT Interaction

When ChatGPT intentionally activates this shared skill, announce
`Skill: investigation-synthesis`, or include it in the responsibility-ordered
composed skill chain, and continue without extra gating.
