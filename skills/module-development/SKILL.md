---
name: module-development
description: Refine a coherent requirement slice into independently constructible modules with narrow contracts, injected clocks/filesystems/repositories/process control/networks, and domain decisions separated from adapters; develop module behavior and its local tests together and submit the slice whole. Do not use for edits with no module boundary.
---

# Module Development

Build software as a small number of independently constructible modules that
each satisfy a refined slice of a current requirement. The unit of design is the
requirement slice; the unit of construction is the module; the unit of
submission is the slice, not the edit.

## Refine The Slice Before Building It

Before implementing a coherent requirement slice, state six things about it:

- **observable behavior:** what an outside caller can see change;
- **inputs and outputs:** the values that cross the boundary, and their shapes;
- **failure behavior:** what it does on invalid input, an unavailable
  dependency, or a violated precondition, and whether it fails closed;
- **dependencies:** what it needs from outside itself;
- **side effects:** what it changes beyond its return value — files, processes,
  network, persistent state, or nothing;
- **sufficient evidence:** the observation that would objectively show the slice
  satisfied, and the observation that would show it violated.

Refine until a competent implementer could build and validate the slice without
inventing material product intent. Use `requirements-driven-development` for the
intent itself; this skill owns only the shape the intent is built into. Stop
refining when those six are settled at the level being executed. Unresolved
ambiguity that could change observable behavior or the satisfying evidence means
the slice is not refined enough; a still-open implementation choice that changes
neither does not.

Do not defer this to implementation and recover it afterward from the code. A
module whose failure behavior and side effects were discovered rather than
decided is the usual source of an untestable boundary.

## Shape Modules As Independently Constructible Units

A module is a unit that can be built, exercised, and reasoned about without
standing up the rest of the system. Give each one a narrow contract: a small
named surface, explicit inputs and outputs, and a single responsibility drawn
from the requirement rather than from the layering diagram.

Prefer a boundary that a caller would recognize as a capability over one that
merely groups code by mechanism. When two modules must know each other's
internals to work, that is one module with a confused seam, not two.

## Inject The World Rather Than Rediscovering It

Domain logic must not reach out for the world. Pass in every ambient capability
at the module boundary:

- **clocks** and any other source of current time;
- **filesystems** and path roots;
- **repositories** and other persistence;
- **process control** — spawning, signalling, and waiting on processes;
- **networks** and remote services.

Inside domain logic, do not read the current time, the working directory, the
environment, a global configuration singleton, or a live connection. A module
that receives these can be exercised with a substitute without patching global
state, running a real clock, or reaching a real service — which is what makes
its tests fast, deterministic, and honest.

Injection is about ownership of the capability, not about a framework. Passing a
function, an object, or a small protocol are all fine. Do not build a container,
a registry, or a lifecycle system to achieve it.

## Keep Domain Decisions Separate From Adapters

Split each module into the part that decides and the part that talks to the
outside world.

- The **decision** takes values in and returns a **structured result** — a value
  describing what is true, or what should happen next — rather than performing
  the effect itself or reporting through logs.
- The **adapter** translates the outside world into those values and carries the
  structured result back out into effects.

Focused tests then assert on the returned structure: the decision, the reason,
the produced records, the refusal. Asserting on a structured result is stable
under refactoring; asserting on captured log text or a sequence of mocked calls
pins the implementation instead of the contract.

When a decision must report a refusal or failure, return it as part of the
structured result. Reserve exceptions for genuinely exceptional conditions, so
that expected failure behavior stays assertable.

## Develop Behavior And Its Tests Together

Work module behavior and its local tests in one fast inner loop. Run the
module's own tests continuously while building; that loop should cost seconds,
which is the real reason to keep the world injected.

**Strict test-first sequencing is not required.** Writing the test first is a
useful technique, not an obligation, and a slice is not deficient because the
implementation sketch preceded its test. What is required is that the slice
arrives with tests that would fail if the claimed behavior were absent, and that
their failure was actually observed rather than assumed.

Keep only tests that earn permanence: a permanent test must protect an accepted
requirement or durable invariant at the narrowest stable boundary that matters,
and a materially different valid implementation of the same accepted contract
should normally continue to pass. Scaffolding written to drive the inner loop —
probes of a chosen internal shape, temporary characterization of a structure
still being decided — is deleted or promoted before submission, not left behind
as regression baggage.

Use `change-validation` to select the evidence tier for the slice. This skill
sets only the inner loop's shape.

## Submit A Coherent Requirement Slice

Integrate a slice, not an edit. A slice is ready to submit when its observable
behavior is complete against the refinement above, its module tests pass, and
the tests for every contract it affected pass.

- Do not treat every small edit as an integration checkpoint. Partial slices
  submitted to buy an early signal produce integration results about states no
  requirement describes.
- Do not accumulate several unrelated slices into one submission either. That
  destroys the change range that makes a failure attributable.
- A slice that grew past one coherent requirement while being built should be
  split before submission, not submitted whole and explained afterward.

Once submitted, integration validation is asynchronous and belongs to
`integration-signal`; continue independent development rather than waiting on
it.

## ChatGPT Interaction

When ChatGPT intentionally activates this shared skill, announce
`Skill: module-development`, or include it in a responsibility-ordered composed
chain, and continue without extra gating.
